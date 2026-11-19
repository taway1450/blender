# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
The socket each key of a built-in node's ``inputs`` and ``outputs`` gives, for the stubs
(see `USE_STUB_GEN` in `sphinx_doc_gen.py`).

A node declares its sockets in C (``node_declare``), which RNA introspection cannot read: the stubs
only know that ``node.inputs["Base Color"]`` is a :class:`bpy.types.NodeSocket`. This module adds
each built-in node to a node tree inside Blender, in each state of the node's own enum and boolean
properties (which may add, remove or retype sockets), and reads the sockets a key finds, as
``rna_NodeInputs_lookup_string`` does: an available socket whose identifier is the key, else one
whose name is. An integer key indexes every socket, available or not.

A key's type is the union, over the states, of the sockets it finds. A node whose sockets come from
elsewhere (a group's tree, a script's file, an add-on's code) has no table: its keys stay
:class:`bpy.types.NodeSocket`. A node with items of its own (a repeat zone's, a menu switch's) is
probed without them, as their names and types are the user's, and its indices are left out, as the
items shift the sockets after them; so are those of a side that a virtual socket extends.
"""

__all__ = (
    "NodeSockets",
    "SocketTable",
    "probe",
)

import itertools
from collections.abc import Iterator
from typing import Any, NamedTuple

import bpy

# Name of every data-block the probe creates.
NAME = "node_socket_probe"

# Nodes whose sockets are not declared by the node type: they come from data the node points to.
DATA_DEFINED_NODES = frozenset((
    "CompositorNodeGroup",
    "GeometryNodeGroup",
    "ShaderNodeGroup",
    "TextureNodeGroup",
    "NodeGroupInput",
    "NodeGroupOutput",
    "ShaderNodeScript",
    "NodeCustomGroup",
))

# Nodes whose sockets are declared from data the node points to (an image's layers), so an index
# names no fixed socket. Their names still do, for the sockets every such node has.
DATA_INDEXED_NODES = frozenset((
    "CompositorNodeImage",
))

# The most states a node is probed in: every combination of its properties' values when there are
# fewer, else each property's values with the others at their defaults and each pair's combinations.
MAX_STATES = 512


class SocketTable(NamedTuple):
    """The socket classes each key of one side (``inputs`` or ``outputs``) of a node finds."""
    # Key -> the struct identifiers of the sockets it finds in some state, in the order found.
    names: dict[str, list[str]]
    # Index -> the struct identifiers of the socket at that index in some state.
    indices: dict[int, list[str]]


class NodeSockets(NamedTuple):
    # Node struct identifier -> (inputs, outputs).
    tables: dict[str, tuple[SocketTable, SocketTable]]
    # The nodes the probe could not add or vary: `"ShaderNodeX: error"`.
    missing: list[str]


def node_classes() -> list[type]:
    """The built-in node classes that are registered node types, sorted by identifier."""
    result = []
    for name in sorted(dir(bpy.types)):
        cls = getattr(bpy.types, name)
        if not (isinstance(cls, type) and issubclass(cls, bpy.types.Node)):
            continue
        if name in DATA_DEFINED_NODES or cls.__module__ != "bpy.types":
            continue
        try:
            if not cls.is_registered_node_type():
                continue
        except Exception:
            continue
        result.append(cls)
    return result


def own_properties(cls: type) -> list[Any]:
    """The writable enum and boolean properties a node class adds to its bases (``ShaderNodeMix.data_type``)."""
    srna = cls.bl_rna
    inherited = set(srna.base.properties.keys()) if srna.base is not None else set()
    return [
        prop for prop in srna.properties
        if prop.identifier not in inherited
        and not prop.is_readonly
        and getattr(prop, "is_array", False) is False
        and (prop.type == 'BOOLEAN' or (prop.type == 'ENUM' and not prop.is_enum_flag))
    ]


def item_collections(node: Any) -> list[Any]:
    """The collections of items a node defines its sockets from (``GeometryNodeRepeatOutput.repeat_items``)."""
    srna = node.bl_rna
    inherited = set(srna.base.properties.keys()) if srna.base is not None else set()
    return [
        getattr(node, prop.identifier) for prop in srna.properties
        if prop.identifier not in inherited and prop.type == 'COLLECTION'
        and hasattr(getattr(node, prop.identifier), "clear")
    ]


def property_values(node: Any, prop: Any) -> list[Any]:
    if prop.type == 'BOOLEAN':
        return [False, True]
    # The items an enum offers for this node (some depend on the tree or the node's state).
    items = node.bl_rna.properties[prop.identifier].enum_items
    return [item.identifier for item in items] or [getattr(node, prop.identifier)]


def states(props: list[tuple[str, list[Any]]]) -> Iterator[dict[str, Any]]:
    """The property values to probe a node in, as {identifier: value}; the empty state is the default one."""
    yield {}
    total = 1
    for _identifier, values in props:
        total *= len(values)
    if total <= MAX_STATES:
        for combination in itertools.product(*(values for _identifier, values in props)):
            yield {identifier: value for (identifier, _values), value in zip(props, combination)}
        return
    for identifier, values in props:
        for value in values:
            yield {identifier: value}
    for (id_a, values_a), (id_b, values_b) in itertools.combinations(props, 2):
        for value_a, value_b in itertools.product(values_a, values_b):
            yield {id_a: value_a, id_b: value_b}


def allows_identifier_lookup(node_id: str) -> bool:
    """Whether ``find_socket_by_key`` tries identifiers first (``allow_identifier_lookup`` in ``rna_nodetree.cc``)."""
    return node_id not in {"ShaderNodeMix", "ShaderNodeMapRange"}


def record_side(table: SocketTable, sockets: Any, *, by_identifier: bool, indexed: bool) -> None:
    def add(mapping: dict[Any, list[str]], key: Any, socket: Any) -> None:
        classes = mapping.setdefault(key, [])
        if (struct_id := socket.bl_rna.identifier) not in classes:
            classes.append(struct_id)

    if indexed:
        for index, socket in enumerate(sockets):
            add(table.indices, index, socket)
    # A virtual socket (``__extend__``) makes a new item when a link is dropped on it: no key names it.
    available = [
        socket for socket in sockets
        if socket.is_unavailable is False and socket.bl_rna.identifier != "NodeSocketVirtual"
    ]
    keys = {socket.name for socket in available}
    if by_identifier:
        keys |= {socket.identifier for socket in available}
    for key in keys:
        found = None
        if by_identifier:
            found = next((socket for socket in available if socket.identifier == key), None)
        if found is None:
            found = next((socket for socket in available if socket.name == key), None)
        if found is not None:
            add(table.names, key, found)


def node_trees(ids: list[Any]) -> list[Any]:
    """A node tree of each kind a built-in node may be added to."""
    def keep(id_data: Any) -> Any:
        ids.append(id_data)
        return id_data

    trees = [keep(bpy.data.node_groups.new(NAME, tree_type)) for tree_type in (
        "ShaderNodeTree", "GeometryNodeTree", "CompositorNodeTree", "TextureNodeTree",
    )]
    # Some shader nodes are only allowed in an object's material, a world or a light.
    material = keep(bpy.data.materials.new(NAME))
    world = keep(bpy.data.worlds.new(NAME))
    light = keep(bpy.data.lights.new(NAME, 'POINT'))
    linestyle = keep(bpy.data.linestyles.new(NAME))
    for id_data in (material, world, light, linestyle):
        if hasattr(id_data, "use_nodes"):
            try:
                id_data.use_nodes = True
            except Exception:
                pass
    trees += [id_data.node_tree for id_data in (material, world, light, linestyle) if id_data.node_tree is not None]
    return trees


def add_node(trees: list[Any], node_id: str) -> Any:
    error: Exception | None = None
    for tree in trees:
        try:
            return tree.nodes.new(node_id)
        except Exception as ex:
            error = ex
    assert error is not None
    raise error


def probe_node(trees: list[Any], cls: type) -> tuple[SocketTable, SocketTable]:
    node_id = cls.bl_rna.identifier
    node = add_node(trees, node_id)
    by_identifier = allows_identifier_lookup(node_id)
    collections = item_collections(node)
    for collection in collections:
        collection.clear()
    indexed = not collections and node_id not in DATA_INDEXED_NODES
    inputs, outputs = SocketTable({}, {}), SocketTable({}, {})
    props = [(prop.identifier, property_values(node, prop)) for prop in own_properties(cls)]
    defaults = {identifier: getattr(node, identifier) for identifier, _values in props}
    for state in states(props):
        for identifier, value in {**defaults, **state}.items():
            try:
                setattr(node, identifier, value)
            except Exception:
                pass
        record_side(inputs, node.inputs, by_identifier=by_identifier, indexed=indexed)
        record_side(outputs, node.outputs, by_identifier=by_identifier, indexed=indexed)
    # An extendable side adds the items a link makes before its virtual socket, shifting the indices after them.
    for table, sockets in ((inputs, node.inputs), (outputs, node.outputs)):
        if any("NodeSocketVirtual" in classes for classes in table.indices.values()):
            table.indices.clear()
    node.id_data.nodes.remove(node)
    return inputs, outputs


def probe() -> NodeSockets:
    """Add each built-in node in each of its states and read the sockets its keys find."""
    result = NodeSockets(tables={}, missing=[])
    ids: list[Any] = []
    try:
        trees = node_trees(ids)
        for cls in node_classes():
            try:
                result.tables[cls.bl_rna.identifier] = probe_node(trees, cls)
            except Exception as ex:
                result.missing.append("{:s}: {:s}".format(cls.bl_rna.identifier, str(ex).strip()))
    finally:
        bpy.data.batch_remove([id_data for id_data in ids if id_data is not None])
    return result
