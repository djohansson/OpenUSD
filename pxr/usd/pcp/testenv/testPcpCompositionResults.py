#!/pxrpythonsubst
#
# Copyright 2017 Pixar
#
# Licensed under the terms set forth in the LICENSE.txt file available at
# https://openusd.org/license.

# This script is a simple driver for the PCP level of the composition
# algorithm.  Given the inputs (currently just a root layer), it
# walks namespace and dumps out the results for every prim.

from __future__ import print_function
import sys, os, argparse, re
from pxr import Pcp, Sdf, Work

# Parse options.
parser = argparse.ArgumentParser()
parser.add_argument('layer', nargs='+',
        help = 'A path to a scene description layer.')
parser.add_argument('--errorFile', dest='errorFileName', 
        help = 'Pcp errors will be outputted into the specified file.')
parser.add_argument('-d', '--dumpPath', dest='dumpPathStr', 
        default=Sdf.Path.emptyPath,
        help = 'Print the internal composition structures for the given path.')
parser.add_argument('--usd', action='store_true',
        dest='usd',
        default=False, help = 'Creates the PcpCache in USD mode.')
parser.add_argument('--layerStackOnly', action='store_true',
        dest='layerStackOnly',
        default=False, help = 'Dump only the layer stack.')
parser.add_argument('--dumpMaps', action='store_true', dest='dumpMaps',
        default=False, help = 'Include the MapFunction when dumping a path.')
parser.add_argument('--variantFallbacks', dest='variantFallbacks', default='',
        help = 'A dictionary of variant fallbacks.  ' +
        'The default {"standin":["render"]}.')
parser.add_argument('--payloads', dest='payloads', default='.*',
        help = 'A regular expression identifying paths of prims where ' +
        'payload should be included.  Paths do not include angle brackets. ' +
        'Default includes every payload.')
parser.add_argument('--session', dest='session', default='',
        help = 'The asset path to the session layer.')
args = parser.parse_args()

# Configure based on the given args.
if args.variantFallbacks:
    variantFallbacks = eval(args.variantFallbacks)
else:
    variantFallbacks = {'standin':['render']}

payloadRegex = re.compile(args.payloads)
hadError = False
errorFile = None
if args.errorFileName:
    errorFile = open(args.errorFileName, "w")
if args.dumpMaps and args.dumpPathStr == '':
    print('--dumpMaps must be used in tandem with --dumpPath')
    sys.exit(0)
dumpPath = Sdf.Path(args.dumpPathStr) if args.dumpPathStr else Sdf.Path()

Work.SetMaximumConcurrencyLimit()

# Error messages and dump strings can contain absolute paths to layers which
# change depending on where the this test command is run. This cleans out the
# absolute test run path from these strings so that baseline compares will work
# regardless of where this is run. 
def CleanTestRunPaths(msg):
    cwd = os.getcwd().replace('\\', '/') + '/'
    return re.compile(re.escape(cwd), re.IGNORECASE).sub('', msg)

def PrintErrorMessage(errorFile, msg):
    if errorFile:
        print(CleanTestRunPaths(msg), file=errorFile)
    else:
        print(CleanTestRunPaths(msg), file=sys.stderr)

def PrintErrors(errorFile, errors):
    global hadError
    hadError = True
    for err in errors:
        PrintErrorMessage(errorFile, str(err))
    PrintErrorMessage(errorFile, '')


# Load the session layer, if any.
sessionLayer = None
if args.session:
    print('Loading session @%s@\n' % args.session)
    sessionLayerPath = args.session
    assert sessionLayerPath
    sessionLayer = Sdf.Layer.FindOrOpen(sessionLayerPath)
    assert sessionLayer

for layerPath in args.layer:
    print('Loading @%s@\n' % layerPath)

    rootLayerPath = layerPath
    assert rootLayerPath

    rootLayer = Sdf.Layer.FindOrOpen(rootLayerPath)
    assert rootLayer

    def GetLayerLabel(layer):
        # Convert backslashes to forward slashes so comparisons
        # against baselines work on Windows
        return (os.path.relpath(layer.realPath,
                                os.path.dirname(rootLayer.realPath))
                .replace("\\", "/"))

    def WalkNodes(node):
        yield node
        for child in node.children:
            for descendant in WalkNodes(child):
                yield descendant

    # Dump the layer stack.
    errors = []
    layerStackId = Pcp.LayerStackIdentifier( rootLayer, sessionLayer )
    pcpCache = Pcp.Cache(layerStackId, usd=args.usd)
    assert pcpCache.GetVariantFallbacks() == {}
    pcpCache.SetVariantFallbacks(variantFallbacks)
    assert pcpCache.GetVariantFallbacks() == variantFallbacks
    (layerStackData, errors) = pcpCache.ComputeLayerStack(layerStackId)
    layerStack = layerStackData.layers

    print('-'*72)
    print('Layer Stack:')
    for layer in layerStack:
        print('    ', GetLayerLabel(layer))
    print('')

    if len(layerStackData.localErrors) > 0:
        PrintErrorMessage(errorFile, '-'*72)
        PrintErrorMessage(errorFile, 'Errors while computing Layer Stack\n')
        PrintErrors(errorFile, layerStackData.localErrors)

    if args.layerStackOnly:
        sys.exit(0)


    prims = []

    if dumpPath:
        # Jump straight to the prim at the given path.
        # First, make sure to load any payloads that might contain it.
        pcpCache.RequestPayloads(dumpPath.GetPrefixes(), [])
        prims = [dumpPath]
    else:
        (pseudoRootIndex, _) = pcpCache.ComputePrimIndex('/')
        prims = [Sdf.Path('/').AppendChild(child) 
                 for child in pseudoRootIndex.ComputePrimChildNames()[0]]

    while prims:
        primPath, prims = prims[0], prims[1:]

        # Check regex for payload inclusion.
        if payloadRegex.match(str(primPath)):
            pcpCache.RequestPayloads([primPath], [])

        # Gather composition results for this prim.
        errors = []
        assert pcpCache.FindPrimIndex(primPath) is None
        (primIndex, primIndexErrors) = pcpCache.ComputePrimIndex( primPath )
        assert pcpCache.FindPrimIndex(primPath) is not None
        errors += primIndexErrors

        primStack = primIndex.primStack
        if len(primStack) == 0:
            continue

        print('-'*72)
        print('Results for composing <%s>' % (primPath))

        # Gather all applied variant selections.
        vselMap = {}
        nodesWithOffsets = []
        allNodes = []
        for node in WalkNodes(primIndex.rootNode):
            if node.path.IsPrimVariantSelectionPath():
                vset, vsel = node.path.GetVariantSelection()
                if vset not in vselMap:
                    vselMap[vset] = vsel
            if (not node.mapToParent.timeOffset.IsIdentity() or
                any([not o.IsIdentity() for o in node.layerStack.layerOffsets])):
                nodesWithOffsets.append(node)
            allNodes.append(node)
        
        # Gather name child and property names
        childNames, prohibitedChildNames = primIndex.ComputePrimChildNames()
        propNames = primIndex.ComputePrimPropertyNames()

        prims = [primPath.AppendChild(child) for child in childNames
                 if child not in prohibitedChildNames] + prims

        # Optionally dump the index for this path.
        if primPath == dumpPath:
            print(CleanTestRunPaths(primIndex.DumpToString(args.dumpMaps)))

        propStackMap = {} 
        targetsMap = {}
        connectionsMap = {}
        deletedTargetPathsMap = {}

        properties = [primPath.AppendProperty(child) for child in propNames]
        while properties:
            propPath, properties = properties[0], properties[1:]

            assert pcpCache.FindPropertyIndex(propPath) is None
            if args.usd:
                # In USD mode, property indexes are not cached so we build it 
                # here.
                (propIndex, propIndexErrors) = \
                    Pcp.BuildPrimPropertyIndex(propPath, pcpCache, primIndex)
                # Building the index does not cache it.
                assert pcpCache.FindPropertyIndex(propPath) is None
            else:
                (propIndex, propIndexErrors) = \
                    pcpCache.ComputePropertyIndex(propPath)
                assert pcpCache.FindPropertyIndex(propPath) is not None

            errors += propIndexErrors

            if len(propIndex.propertyStack) == 0:
                continue

            propStackMap[propPath] = propIndex.propertyStack

            if isinstance(propIndex.propertyStack[0], Sdf.RelationshipSpec):
                (targets, deletedPaths, targetErrors) = \
                    pcpCache.ComputeRelationshipTargetPaths(propPath)
                errors += targetErrors
                if len(targets) > 0:
                    targetsMap[propPath] = targets
                if deletedPaths:
                    deletedTargetPathsMap[propPath] = deletedPaths
            elif isinstance(propIndex.propertyStack[0], Sdf.AttributeSpec):
                (conns, deletedPaths, connErrors) = \
                    pcpCache.ComputeAttributeConnectionPaths(propPath)
                errors += connErrors
                if len(conns) > 0:
                    connectionsMap[propPath] = conns
                if deletedPaths:
                    deletedTargetPathsMap[propPath] = deletedPaths

        print('\nPrim Stack:')
        for primSpec in primStack:
            # Determine a short form of the spec's layer's path.
            layerLabel = GetLayerLabel(primSpec.layer)
            print('    %-20s %s' % (layerLabel, primSpec.path))

        if len(nodesWithOffsets) > 0:
            print('\nTime Offsets:')
            for node in allNodes:
                print('    %-20s %-15s %-10s (offset=%.2f, scale=%.2f)' % \
                    (GetLayerLabel(node.layerStack.layers[0]),
                     node.path, node.arcType.displayName,
                     node.mapToRoot.timeOffset.offset, 
                     node.mapToRoot.timeOffset.scale))
                
                for (layer, offset) in zip(node.layerStack.layers, 
                                           node.layerStack.layerOffsets):
                    if not offset.IsIdentity():
                        print('        %-32s %-10s (offset=%.2f, ' \
                            'scale=%.2f)' % \
                            (GetLayerLabel(layer),
                             'sublayer',
                             offset.offset, offset.scale))

        if len(vselMap) > 0:
            print('\nVariant Selections:')
            for vsetName in sorted(vselMap.keys()):
                print('    {%s = %s}' % (vsetName, vselMap[vsetName]))

        if len(childNames) > 0:
            print('\nChild names:')
            print('    ', childNames)

        if len(prohibitedChildNames) > 0:
            print('\nProhibited child names:')
            # Write the set of prohibited names in stable (sorted) order.
            print('    ', sorted(prohibitedChildNames))

        if len(propNames) > 0:
            print('\nProperty names:')
            print('    ', propNames)

        if len(propStackMap) > 0:
            print('\nProperty stacks:')
            for propPath in sorted(propStackMap.keys()):
                print('%s:' % (propPath))
                for propSpec in propStackMap[propPath]:
                    # Determine a short form of the spec's layer's path.
                    layerLabel = GetLayerLabel(propSpec.layer)
                    print('    %-20s %s' % (layerLabel, propSpec.path))

        def _PrintTargets(targetMap):
            for propPath in sorted(targetMap.keys()):
                print('%s:' % (propPath))
                for targetPath in targetMap[propPath]:
                    print('    %s' % targetPath)
                    # Target paths should never include variant selections.
                    # Variant selections are part of addressing layer
                    # opinion storage (like the asset path); they are
                    # not a feature of composed scene namespace.
                    assert not targetPath.ContainsPrimVariantSelection(), \
                        'Target path %s has variant selections' % targetPath

        if len(targetsMap) > 0:
            print('\nRelationship targets:')
            _PrintTargets(targetsMap)

        if len(connectionsMap) > 0:
            print('\nAttribute connections:')
            _PrintTargets(connectionsMap)

        if deletedTargetPathsMap:
            print('\nDeleted target paths:')
            _PrintTargets(deletedTargetPathsMap)
            
        # Print out errors encountered while composing this prim.
        if len(errors) > 0:
            PrintErrorMessage(errorFile, '-'*72)
            PrintErrorMessage(errorFile, 
                              'Errors while composing <%s>\n' % (primPath))
            PrintErrors(errorFile, errors)

        print('')

        # Stop after we hit the path to dump.
        if primPath == dumpPath:
            sys.exit(0)

if errorFile:
    errorFile.close()

if hadError and not errorFile:
    print("ERROR: Unexpected error(s) encountered during test!", file=sys.stderr)
    sys.exit(1)
