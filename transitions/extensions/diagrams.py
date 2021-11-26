"""
    transitions.extensions.diagrams
    -------------------------------

    This module contains machine and transition definitions for generating diagrams from machine instances.
    It uses Graphviz either directly with the help of pygraphviz (https://pygraphviz.github.io/) or loosely
    coupled via dot graphs with the graphviz module (https://github.com/xflr6/graphviz).
    Pygraphviz accesses libgraphviz directly and also features more functionality considering graph manipulation.
    However, especially on Windows, compiling the required extension modules can be tricky.
    Furthermore, some pygraphviz issues are platform-dependent as well.
    Graphviz generates a dot graph and calls the `dot` executable to generate diagrams and thus is commonly easier to
    set up. Make sure that the `dot` executable is in your PATH.
"""

import logging
from functools import partial
import copy
import abc

import six

from transitions import Transition
from transitions.extensions.markup import MarkupMachine, HierarchicalMarkupMachine
from transitions.extensions.nesting import NestedTransition

from transitions.core import listify


_LOGGER = logging.getLogger(__name__)
_LOGGER.addHandler(logging.NullHandler())


class TransitionGraphSupport(Transition):
    """ Transition used in conjunction with (Nested)Graphs to update graphs whenever a transition is
        conducted.
    """

    def __init__(self, *args, **kwargs):
        label = kwargs.pop("label", None)
        super(TransitionGraphSupport, self).__init__(*args, **kwargs)
        if label:
            self.label = label

    def _change_state(self, event_data):
        graph = event_data.machine.model_graphs[id(event_data.model)]
        graph.reset_styling()
        graph.set_previous_transition(self.source, self.dest)
        super(TransitionGraphSupport, self)._change_state(
            event_data
        )  # pylint: disable=protected-access
        graph = event_data.machine.model_graphs[
            id(event_data.model)
        ]  # graph might have changed during change_event
        for state in _flatten(
            listify(getattr(event_data.model, event_data.machine.model_attribute))
        ):
            graph.set_node_style(self.dest if hasattr(state, "name") else state, "active")


class GraphMachine(MarkupMachine):
    """ Extends transitions.core.Machine with graph support.
        Is also used as a mixin for HierarchicalMachine.
        Attributes:
            _pickle_blacklist (list): Objects that should not/do not need to be pickled.
            transition_cls (cls): TransitionGraphSupport
    """

    _pickle_blacklist = ["model_graphs"]
    transition_cls = TransitionGraphSupport

    machine_attributes = {
        "directed": "true",
        "strict": "false",
        "rankdir": "LR",
    }

    hierarchical_machine_attributes = {
        "rankdir": "TB",
        "rank": "source",
        "nodesep": "1.5",
        "compound": "true",
    }

    style_attributes = {
        "node": {
            "": {},
            "default": {
                "style": "rounded, filled",
                "shape": "rectangle",
                "fillcolor": "white",
                "color": "black",
                "peripheries": "1",
            },
            "inactive": {"fillcolor": "white", "color": "black", "peripheries": "1"},
            "parallel": {
                "shape": "rectangle",
                "color": "black",
                "fillcolor": "white",
                "style": "dashed, rounded, filled",
                "peripheries": "1",
            },
            "active": {"color": "red", "fillcolor": "darksalmon", "peripheries": "2"},
            "previous": {"color": "blue", "fillcolor": "azure2", "peripheries": "1"},
        },
        "edge": {"": {}, "default": {"color": "black"}, "previous": {"color": "blue"}},
        "graph": {
            "": {},
            "default": {"color": "black", "fillcolor": "white", "style": "solid"},
            "previous": {"color": "blue", "fillcolor": "azure2", "style": "filled"},
            "active": {"color": "red", "fillcolor": "darksalmon", "style": "filled"},
            "parallel": {"color": "black", "fillcolor": "white", "style": "dotted"},
        },
    }

    # model_graphs cannot be pickled. Omit them.
    def __getstate__(self):
        # self.pkl_graphs = [(g.markup, g.custom_styles) for g in self.model_graphs]
        return {k: v for k, v in self.__dict__.items() if k not in self._pickle_blacklist}

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.model_graphs = {}  # reinitialize new model_graphs
        for model in self.models:
            try:
                _ = self._get_graph(model)
            except AttributeError as err:
                _LOGGER.warning("Graph for model could not be initialized after pickling: %s", err)

    def __init__(self, *args, **kwargs):
        # remove graph config from keywords
        self.title = kwargs.pop("title", "State Machine")
        self.show_conditions = kwargs.pop("show_conditions", False)
        self.show_state_attributes = kwargs.pop("show_state_attributes", False)
        # in MarkupMachine this switch is called 'with_auto_transitions'
        # keep 'auto_transitions_markup' for backwards compatibility
        kwargs["auto_transitions_markup"] = kwargs.get(
            "auto_transitions_markup", False
        ) or kwargs.pop("show_auto_transitions", False)
        self.model_graphs = {}
        self.graph_cls = self._init_graphviz_engine(kwargs.pop("use_pygraphviz", True))

        _LOGGER.debug("Using graph engine %s", self.graph_cls)
        super(GraphMachine, self).__init__(*args, **kwargs)

        # for backwards compatibility assign get_combined_graph to get_graph
        # if model is not the machine
        if not hasattr(self, "get_graph"):
            setattr(self, "get_graph", self.get_combined_graph)

    def _init_graphviz_engine(self, use_pygraphviz):
        """ Imports diagrams (py)graphviz backend based on machine configuration """
        if use_pygraphviz:
            try:
                # state class needs to have a separator and machine needs to be a context manager
                if hasattr(self.state_cls, "separator") and hasattr(self, "__enter__"):
                    from .diagrams_pygraphviz import (  # pylint: disable=import-outside-toplevel
                        NestedGraph as Graph,
                    )

                    self.machine_attributes.update(self.hierarchical_machine_attributes)
                else:
                    from .diagrams_pygraphviz import (  # pylint: disable=import-outside-toplevel
                        Graph,
                    )
                return Graph
            except ImportError:
                _LOGGER.warning("%sCould not import pygraphviz backend. Will try graphviz backend next", self.name)
        if hasattr(self.state_cls, "separator") and hasattr(self, "__enter__"):
            from .diagrams_graphviz import (  # pylint: disable=import-outside-toplevel
                NestedGraph as Graph,
            )

            self.machine_attributes.update(self.hierarchical_machine_attributes)
        else:
            from .diagrams_graphviz import Graph  # pylint: disable=import-outside-toplevel
        return Graph

    def _get_graph(self, model, title=None, force_new=False, show_roi=False):
        """ This method will be bound as a partial to models and return a graph object to be drawn or manipulated.
        Args:
            model (object): The model that `_get_graph` was bound to. This parameter will be set by `GraphMachine`.
            title (str): The title of the created graph.
            force_new (bool): Whether a new graph should be generated even if another graph already exists. This should
            be true whenever the model's state or machine's transitions/states/events have changed.
            show_roi (bool): If set to True, only render states that are active and/or can be reached from
                the current state.
        Returns: AGraph (pygraphviz) or Digraph (graphviz) graph instance that can be drawn.
        """
        if force_new:
            grph = self.graph_cls(self)
            self.model_graphs[id(model)] = grph
            try:
                for state in _flatten(listify(getattr(model, self.model_attribute))):
                    grph.set_node_style(self.dest if hasattr(state, "name") else state, "active")
            except AttributeError:
                _LOGGER.info("Could not set active state of diagram")
        try:
            grph = self.model_graphs[id(model)]
        except KeyError:
            _ = self._get_graph(model, title, force_new=True)
            grph = self.model_graphs[id(model)]
        return grph.get_graph(title=title, roi_state=getattr(model, self.model_attribute) if show_roi else None)

    def get_combined_graph(self, title=None, force_new=False, show_roi=False):
        """ This method is currently equivalent to 'get_graph' of the first machine's model.
        In future releases of transitions, this function will return a combined graph with active states
        of all models.
        Args:
            title (str): Title of the resulting graph.
            force_new (bool): Whether a new graph should be generated even if another graph already exists. This should
            be true whenever the model's state or machine's transitions/states/events have changed.
            show_roi (bool): If set to True, only render states that are active and/or can be reached from
                the current state.
        Returns: AGraph (pygraphviz) or Digraph (graphviz) graph instance that can be drawn.
        """
        _LOGGER.info(
            "Returning graph of the first model. In future releases, this "
            "method will return a combined graph of all models."
        )
        return self._get_graph(self.models[0], title, force_new, show_roi)

    def add_model(self, model, initial=None):
        models = listify(model)
        super(GraphMachine, self).add_model(models, initial)
        for mod in models:
            mod = self if mod is self.self_literal else mod
            if hasattr(mod, "get_graph"):
                raise AttributeError(
                    "Model already has a get_graph attribute. Graph retrieval cannot be bound."
                )
            setattr(mod, "get_graph", partial(self._get_graph, mod))
            _ = mod.get_graph(title=self.title, force_new=True)  # initialises graph

    def add_states(
        self, states, on_enter=None, on_exit=None, ignore_invalid_triggers=None, **kwargs
    ):
        """ Calls the base method and regenerates all models's graphs. """
        super(GraphMachine, self).add_states(
            states,
            on_enter=on_enter,
            on_exit=on_exit,
            ignore_invalid_triggers=ignore_invalid_triggers,
            **kwargs
        )
        for model in self.models:
            model.get_graph(force_new=True)

    def add_transition(self, trigger, source, dest, conditions=None, unless=None, before=None, after=None,
                       prepare=None, **kwargs):
        """ Calls the base method and regenerates all models's graphs. """
        super(GraphMachine, self).add_transition(trigger, source, dest, conditions=conditions, unless=unless,
                                                 before=before, after=after, prepare=prepare, **kwargs)
        for model in self.models:
            model.get_graph(force_new=True)


class NestedGraphTransition(TransitionGraphSupport, NestedTransition):
    """
        A transition type to be used with (subclasses of) `HierarchicalGraphMachine` and
        `LockedHierarchicalGraphMachine`.
    """


class HierarchicalGraphMachine(GraphMachine, HierarchicalMarkupMachine):
    """
        A hierarchical state machine with graph support.
    """

    transition_cls = NestedGraphTransition


@six.add_metaclass(abc.ABCMeta)
class BaseGraph(object):
    """ Provides the common foundation for graphs generated either with pygraphviz or graphviz. This abstract class
    should not be instantiated directly. Use .(py)graphviz.(Nested)Graph instead.
    Attributes:
        machine (GraphMachine): The associated GraphMachine
        fsm_graph (object): The AGraph-like object that holds the graphviz information
    """

    def __init__(self, machine):
        self.machine = machine
        self.fsm_graph = None
        self.generate()

    @abc.abstractmethod
    def generate(self):
        """ Triggers the generation of a graph. """

    @abc.abstractmethod
    def set_previous_transition(self, src, dst):
        """ Sets the styling of an edge to 'previous'
        Args:
            src (str): Name of the source state
            dst (str): Name of the destination
        """

    @abc.abstractmethod
    def reset_styling(self):
        """ Resets the styling of the currently generated graph. """

    @abc.abstractmethod
    def set_node_style(self, state, style):
        """ Sets the style of a node state
        Args:
            state (str): Name of the state
            style (str): Name of the style
        """

    @abc.abstractmethod
    def get_graph(self, title=None, roi_state=None):
        """ Returns a graph object.
        Args:
            title (str): Title of the generated graph
            roi_state (State): If not None, the returned graph will only contain edges and states connected to it.
        Returns:
             A graph instance with a `draw` that allows to render the graph.
        """

    def _convert_state_attributes(self, state):
        label = state.get("label", state["name"])
        if self.machine.show_state_attributes:
            if "tags" in state:
                label += " [" + ", ".join(state["tags"]) + "]"
            if "on_enter" in state:
                label += r"\l- enter:\l  + " + r"\l  + ".join(state["on_enter"])
            if "on_exit" in state:
                label += r"\l- exit:\l  + " + r"\l  + ".join(state["on_exit"])
            if "timeout" in state:
                label += r'\l- timeout(' + state['timeout'] + 's) -> (' + ', '.join(state['on_timeout']) + ')'
        return label

    def _transition_label(self, tran):
        edge_label = tran.get("label", tran["trigger"])
        if "dest" not in tran:
            edge_label += " [internal]"
        if self.machine.show_conditions and any(prop in tran for prop in ["conditions", "unless"]):
            edge_label = "{edge_label} [{conditions}]".format(
                edge_label=edge_label,
                conditions=" & ".join(
                    tran.get("conditions", []) + ["!" + u for u in tran.get("unless", [])]
                ),
            )
        return edge_label

    def _get_global_name(self, path):
        if path:
            state = path.pop(0)
            with self.machine(state):
                return self._get_global_name(path)
        else:
            return self.machine.get_global_name()

    def _get_elements(self):
        states = []
        transitions = []
        try:
            markup = self.machine.get_markup_config()
            queue = [([], markup)]

            while queue:
                prefix, scope = queue.pop(0)
                for transition in scope.get("transitions", []):
                    if prefix:
                        tran = copy.copy(transition)
                        tran["source"] = self.machine.state_cls.separator.join(
                            prefix + [tran["source"]]
                        )
                        if "dest" in tran:  # don't do this for internal transitions
                            tran["dest"] = self.machine.state_cls.separator.join(
                                prefix + [tran["dest"]]
                            )
                    else:
                        tran = transition
                    transitions.append(tran)
                for state in scope.get("children", []) + scope.get("states", []):
                    if not prefix:
                        sta = state
                        states.append(sta)

                    ini = state.get("initial", [])
                    if not isinstance(ini, list):
                        ini = ini.name if hasattr(ini, "name") else ini
                        tran = dict(
                            trigger="",
                            source=self.machine.state_cls.separator.join(prefix + [state["name"]]) + "_anchor",
                            dest=self.machine.state_cls.separator.join(
                                prefix + [state["name"], ini]
                            ),
                        )
                        transitions.append(tran)
                    if state.get("children", []):
                        queue.append((prefix + [state["name"]], state))
        except KeyError:
            _LOGGER.error("Graph creation incomplete!")
        return states, transitions


def _flatten(item):
    for elem in item:
        if isinstance(elem, (list, tuple, set)):
            for res in _flatten(elem):
                yield res
        else:
            yield elem
