# Copyright 2016, Yahoo Inc.
# Licensed under the terms of the Apache License, Version 2.0. See the LICENSE file associated with the project for terms.

import time
import os
import networkx as nx

from io import StringIO

from .base import Operation, NetworkOperation, Control


class DataPlaceholderNode(str):
    """
    A node for the Network graph that describes the name of a Data instance
    produced or required by a layer.
    """
    def __repr__(self):
        return 'DataPlaceholderNode("%s")' % self


class DeleteInstruction(str):
    """
    An instruction for the compiled list of evaluation steps to free or delete
    a Data instance from the Network's cache after it is no longer needed.
    """
    def __repr__(self):
        return 'DeleteInstruction("%s")' % self


class Network(object):
    """
    This is the main network implementation. The class contains all of the
    code necessary to weave together operations into a directed-acyclic-graph (DAG)
    and pass data through.
    """

    def __init__(self, **kwargs):
        """
        """

        # directed graph of layer instances and data-names defining the net.
        self.graph = nx.DiGraph()
        self._debug = kwargs.get("debug", False)

        # this holds the timing information for eache layer
        self.times = {}

        # a compiled list of steps to evaluate layers *in order* and free mem.
        self.steps = []

        # This holds a cache of results for the _find_necessary_steps
        # function, this helps speed up the compute call as well avoid
        # a multithreading issue that is occuring when accessing the
        # graph in networkx
        self._necessary_steps_cache = {}

    def add_op(self, operation):
        """
        Adds the given operation and its data requirements to the network graph
        based on the name of the operation, the names of the operation's needs, and
        the names of the data it provides.

        :param Operation operation: Operation object to add.
        """

        # assert layer and its data requirements are named.
        assert operation.name, "Operation must be named"
        assert operation.needs is not None, "Operation's 'needs' must be named"
        assert operation.provides is not None, "Operation's 'provides' must be named"

        # assert layer is only added once to graph
        assert operation not in self.graph.nodes(), "Operation may only be added once"

        # add nodes and edges to graph describing the data needs for this layer
        for n in operation.needs:
            self.graph.add_edge(DataPlaceholderNode(n.name), operation)

            if 'type' not in self.graph.nodes[n.name]:
                self.graph.nodes[n.name]['type'] = n.type
            elif self.graph.nodes[n.name]['type'] != n.type:
                raise TypeError("Duplicate nodes with different types. Needs: %s Expected: %s Got: %s" %
                                (n.name, self.graph.nodes[n.name]['type'], n.type))

        # add nodes and edges to graph describing what this layer provides
        for p in operation.provides:
            self.graph.add_edge(operation, DataPlaceholderNode(p.name))

            if 'type' not in self.graph.nodes[p.name]:
                self.graph.nodes[p.name]['type'] = p.type
            elif self.graph.nodes[p.name]['type'] != p.type:
                raise TypeError("Duplicate nodes with different types. Provides: %s Expected: %s Got: %s" %
                                (p.name, self.graph.nodes[p.name]['type'], p.type))

        if operation.color:
            self.graph.nodes[operation]['color'] = operation.color

        if isinstance(operation, Control) and hasattr(operation, 'condition_needs'):
            for n in operation.condition_needs:
                self.graph.add_edge(DataPlaceholderNode(n), operation)

        # clear compiled steps (must recompile after adding new layers)
        self.steps = []

    def list_layers(self):
        assert self.steps, "network must be compiled before listing layers."
        return [(s.name, s) for s in self.steps if isinstance(s, Operation)]

    def show_layers(self):
        """Shows info (name, needs, and provides) about all layers in this network."""
        for name, step in self.list_layers():
            print("layer_name: ", name)
            print("\t", "needs: ", step.needs)
            print("\t", "provides: ", step.provides)
            print("\t", "color: ", step.color)
            if hasattr(step, 'condition_needs'):
                print("\t", "condition needs: ", step.condition_needs)
            print("")

    def compile(self):
        """Create a set of steps for evaluating layers
           and freeing memory as necessary"""

        # clear compiled steps
        self.steps = []

        # create an execution order such that each layer's needs are provided.
        try:
            def key(node):

                if hasattr(node, 'order'):
                    return node.order
                elif isinstance(node, DataPlaceholderNode):
                    return float('-inf')
                else:
                    return 0

            ordered_nodes = list(nx.dag.lexicographical_topological_sort(self.graph,
                                                                         key=key))
        except TypeError as e:
            if self._debug:
                print("Lexicographical topological sort failed! Falling back to topological sort.")

            if not any(map(lambda node: isinstance(node, Control), self.graph.nodes)):
                ordered_nodes = list(nx.dag.topological_sort(self.graph))
            else:
                print("Topological sort failed!")
                raise e

        # add Operations evaluation steps, and instructions to free data.
        for i, node in enumerate(ordered_nodes):

            if isinstance(node, DataPlaceholderNode):
                continue

            elif isinstance(node, Control):
                self.steps.append(node)

            elif isinstance(node, Operation):

                # add layer to list of steps
                self.steps.append(node)

                # Add instructions to delete predecessors as possible.  A
                # predecessor may be deleted if it is a data placeholder that
                # is no longer needed by future Operations.
                for predecessor in self.graph.predecessors(node):

                    if self._debug:
                        print("checking if node %s can be deleted" % predecessor)

                    predecessor_still_needed = False
                    for future_node in ordered_nodes[i+1:]:
                        if isinstance(future_node, Operation):
                            if predecessor in map(lambda arg: arg.name, future_node.needs):
                                predecessor_still_needed = True
                                break
                    if not predecessor_still_needed:
                        if self._debug:
                            print("  adding delete instruction for %s" % predecessor)
                        self.steps.append(DeleteInstruction(predecessor))

            else:
                raise TypeError("Unrecognized network graph node")

    def _find_necessary_steps(self, outputs, inputs, color=None):
        """
        Determines what graph steps need to be run to get to the requested
        outputs from the provided inputs.  Eliminates steps that come before
        (in topological order) any inputs that have been provided.  Also
        eliminates steps that are not on a path from the provided inputs to
        the requested outputs.

        :param list outputs:
            A list of desired output names.  This can also be ``None``, in which
            case the necessary steps are all graph nodes that are reachable
            from one of the provided inputs.

        :param dict inputs:
            A dictionary mapping names to values for all provided inputs.

        :param str color:
            A color to filter nodes by.

        :returns:
            Returns a list of all the steps that need to be run for the
            provided inputs and requested outputs.
        """

        # return steps if it has already been computed before for this set of inputs and outputs
        outputs = tuple(sorted(outputs)) if isinstance(outputs, (list, set)) else outputs
        inputs_keys = tuple(sorted(inputs.keys()))
        cache_key = (*inputs_keys, outputs, color)
        if cache_key in self._necessary_steps_cache:
            return self._necessary_steps_cache[cache_key]
        graph = self.graph
        if not outputs:

            # If caller requested all outputs, the necessary nodes are all
            # nodes that are reachable from one of the inputs.  Ignore input
            # names that aren't in the graph.
            necessary_nodes = set()
            subgraphs = list(filter(lambda node: isinstance(node, NetworkOperation) or isinstance(node, Control), graph.nodes))
            for input_name in iter(inputs):
                if graph.has_node(input_name):
                    necessary_nodes |= nx.descendants(graph, input_name)
                for subgraph in subgraphs:
                    if isinstance(subgraph, NetworkOperation) and subgraph.net.graph.has_node(input_name):
                        necessary_nodes.add(subgraph)
                    elif isinstance(subgraph, Control) and subgraph.graph.net.graph.has_node(input_name):
                        necessary_nodes.add(subgraph)
        else:

            # If the caller requested a subset of outputs, find any nodes that
            # are made unecessary because we were provided with an input that's
            # deeper into the network graph.  Ignore input names that aren't
            # in the graph.
            unnecessary_nodes = set()
            for input_name in iter(inputs):
                if graph.has_node(input_name):
                    unnecessary_nodes |= nx.ancestors(graph, input_name)

            # Find the nodes we need to be able to compute the requested
            # outputs.  Raise an exception if a requested output doesn't
            # exist in the graph.
            necessary_nodes = set()
            for output_name in outputs:
                if not graph.has_node(output_name):
                    raise ValueError("graphkit graph does not have an output "
                                     "node named %s" % output_name)
                necessary_nodes |= nx.ancestors(graph, output_name)

            # Get rid of the unnecessary nodes from the set of necessary ones.
            necessary_nodes -= unnecessary_nodes

        necessary_steps = []

        for step in self.steps:
            if isinstance(step, Operation):
                if step.color == color and step in necessary_nodes:
                    necessary_steps.append(step)
                elif isinstance(step, Control) and step in necessary_nodes:
                    necessary_steps.append(step)
            else:
                if step in necessary_nodes:
                    necessary_steps.append(step)

        # save this result in a precomputed cache for future lookup
        self._necessary_steps_cache[cache_key] = necessary_steps

        # Return an ordered list of the needed steps.
        return necessary_steps

    def compute(self, outputs, named_inputs, color=None):
        """
        This method runs the graph one operation at a time in a single thread
        Any inputs to the network must be passed in by name.

        :param list output: The names of the data node you'd like to have returned
                            once all necessary computations are complete.
                            If you set this variable to ``None``, all
                            data nodes will be kept and returned at runtime.

        :param dict named_inputs: A dict of key/value pairs where the keys
                                  represent the data nodes you want to populate,
                                  and the values are the concrete values you
                                  want to set for the data node.

        :param str color: Only the subgraph of nodes with color will be evaluted.

        :returns: a dictionary of output data objects, keyed by name.
        """

        # assert that network has been compiled
        assert self.steps, "network must be compiled before calling compute."
        assert isinstance(outputs, (list, tuple)) or outputs is None,\
            "The outputs argument must be a list"

        # start with fresh data cache
        cache = {}

        # add inputs to data cache
        cache.update(named_inputs)

        # Find the subset of steps we need to run to get to the requested
        # outputs from the provided inputs.
        all_steps = self._find_necessary_steps(outputs, named_inputs, color)

        self.times = {}
        if_true = False

        for step in all_steps:

            if isinstance(step, Control):
                if hasattr(step, 'condition'):
                    if all(map(lambda need: need in cache, step.condition_needs)):
                        if_true = step._compute_condition(cache)
                        if if_true:
                            layer_outputs = step._compute(cache, color)
                            cache.update(layer_outputs)
                    else:
                        # assume short circuiting if statement
                        layer_outputs = step._compute(cache, color)
                        cache.update(layer_outputs)
                elif not if_true:
                    layer_outputs = step._compute(cache, color)
                    cache.update(layer_outputs)
                    if_true = False

            elif isinstance(step, Operation):

                if self._debug:
                    print("-"*32)
                    print("executing step: %s" % step.name)

                # time execution...
                t0 = time.time()

                # compute layer outputs
                layer_outputs = step._compute(cache)

                for output in step.provides:
                    if output.name in layer_outputs and not isinstance(layer_outputs[output.name], output.type):
                        raise TypeError("Type mismatch. Operation: %s Output: %s Expected: %s Got: %s" %
                                        (step.name, output.name, output.type, type(layer_outputs[output.name])))

                # add outputs to cache
                cache.update(layer_outputs)

                # record execution time
                t_complete = round(time.time() - t0, 5)
                self.times[step.name] = t_complete
                if self._debug:
                    print("step completion time: %s" % t_complete)

            # Process DeleteInstructions by deleting the corresponding data
            # if possible.
            elif isinstance(step, DeleteInstruction):

                if outputs and step not in outputs:
                    # Some DeleteInstruction steps may not exist in the cache
                    # if they come from optional() needs that are not privoded
                    # as inputs.  Make sure the step exists before deleting.
                    if step in cache:
                        if self._debug:
                            print("removing data '%s' from cache." % step)
                        cache.pop(step)

            else:
                raise TypeError("Unrecognized instruction.")

        if not outputs:
            # Return cache as output including intermediate data nodes,
            # but excluding input.
            return {k: cache[k] for k in set(cache) - set(named_inputs)}

        else:
            # Filter outputs to just return what's needed.
            # Note: list comprehensions exist in python 2.7+
            return {k: cache[k] for k in iter(cache) if k in outputs}

    def plot(self, filename=None, show=False):
        """
        Plot the graph.

        params:
        :param str filename:
            Write the output to a png, pdf, or graphviz dot file. The extension
            controls the output format.

        :param boolean show:
            If this is set to True, use matplotlib to show the graph diagram
            (Default: False)

        :returns:
            An instance of the pydot graph

        """
        import pydot
        import matplotlib.pyplot as plt
        import matplotlib.image as mpimg

        assert self.graph is not None

        def get_node_name(a):
            if isinstance(a, DataPlaceholderNode):
                return a
            return a.name

        g = pydot.Dot(graph_type="digraph")

        # draw nodes
        for nx_node in self.graph.nodes():
            if isinstance(nx_node, DataPlaceholderNode):
                node = pydot.Node(name=nx_node, shape="rect")
            else:
                node = pydot.Node(name=nx_node.name, shape="circle")
            g.add_node(node)

        # draw edges
        for src, dst in self.graph.edges():
            src_name = get_node_name(src)
            dst_name = get_node_name(dst)
            edge = pydot.Edge(src=src_name, dst=dst_name)
            g.add_edge(edge)

        # save plot
        if filename:
            basename, ext = os.path.splitext(filename)
            with open(filename, "wb") as fh:
                if ext.lower() == ".png":
                    fh.write(g.create_png())
                elif ext.lower() == ".dot":
                    fh.write(g.to_string())
                elif ext.lower() in [".jpg", ".jpeg"]:
                    fh.write(g.create_jpeg())
                elif ext.lower() == ".pdf":
                    fh.write(g.create_pdf())
                elif ext.lower() == ".svg":
                    fh.write(g.create_svg())
                else:
                    raise Exception("Unknown file format for saving graph: %s" % ext)

        # display graph via matplotlib
        if show:
            png = g.create_png()
            sio = StringIO(png)
            img = mpimg.imread(sio)
            plt.imshow(img, aspect="equal")
            plt.show()

        return g
