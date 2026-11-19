# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
The socket each key of a built-in node's ``inputs`` and ``outputs`` gives, for the stubs
(see `USE_STUB_GEN` in `sphinx_doc_gen.py`).

A node declares its sockets in C (``node_declare``), which RNA introspection cannot read: the stubs
only know that ``node.inputs["Base Color"]`` is a :class:`bpy.types.NodeSocket`. This module adds
each built-in node to a node tree inside Blender, in the states of the node's own enum and boolean
properties and of its menu input sockets (which may add, remove or retype sockets; see `states`), and
reads the sockets a key finds, as ``rna_NodeInputs_lookup_string`` does: an available socket whose identifier is the key, else one
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

import ast
import itertools
import re
from collections.abc import Callable, Iterator
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


def node_classes() -> list[type[bpy.types.Node]]:
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


def own_rna_properties(struct: Any) -> list[Any]:
    """The RNA properties a struct's class adds to its base's (``ShaderNodeMix.data_type``)."""
    srna = struct.bl_rna
    inherited = set(srna.base.properties.keys()) if srna.base is not None else set()
    return [prop for prop in srna.properties if prop.identifier not in inherited]


def item_collections(node: Any) -> list[Any]:
    """The collections of items a node defines its sockets from (``GeometryNodeRepeatOutput.repeat_items``)."""
    return [
        getattr(node, prop.identifier) for prop in own_rna_properties(node)
        if prop.type == 'COLLECTION' and hasattr(getattr(node, prop.identifier), "clear")
    ]


def instance_enum_items(owner: Any, attr: str) -> list[str]:
    """
    The items the enum ``owner.attr`` offers this very struct. ``bl_rna.properties[attr].enum_items`` reads a
    dynamic enum without the struct, so it gives the static items or none: Blender lists the instance's items in
    the error of an assignment of an unknown one.
    """
    try:
        setattr(owner, attr, "\x01")
    except TypeError as ex:
        if (match := re.search(r"not found in (\(.*\))", str(ex))) is not None:
            # A single item is written without a trailing comma: `('ViewLayer')`.
            items = ast.literal_eval(match.group(1))
            return [items] if isinstance(items, str) else [item for item in items if isinstance(item, str)]
    return []


class Variant(NamedTuple):
    """A value a node's sockets may depend on: one of its own enum or boolean properties, or a menu input socket."""
    name: str
    # Read and write the value; `read` raises `LookupError` when the state has no such value (a menu socket the
    # node does not declare in this state).
    read: Callable[[], Any]
    write: Callable[[Any], None]
    values: list[Any]
    default: Any


def property_variant(node: Any, identifier: str, values: list[Any]) -> Variant:
    return Variant(
        identifier,
        lambda: getattr(node, identifier),
        lambda value: setattr(node, identifier, value),
        values,
        getattr(node, identifier),
    )


def menu_socket_variant(node: Any, socket: Any) -> Variant:
    identifier = socket.identifier

    def find() -> Any:
        for candidate in node.inputs:
            if candidate.identifier == identifier and candidate.bl_rna.identifier == "NodeSocketMenu":
                return candidate
        raise LookupError(identifier)

    return Variant(
        "inputs[{!r}]".format(identifier),
        lambda: find().default_value,
        lambda value: setattr(find(), "default_value", value),
        instance_enum_items(socket, "default_value"),
        socket.default_value,
    )


def node_variants(node: Any) -> list[Variant]:
    """The writable enum and boolean properties of a node's own class, then its menu input sockets."""
    variants = []
    for prop in own_rna_properties(node):
        if prop.is_readonly or getattr(prop, "is_array", False):
            continue
        if prop.type == 'BOOLEAN':
            variants.append(property_variant(node, prop.identifier, [False, True]))
        elif prop.type == 'ENUM' and not prop.is_enum_flag:
            values = instance_enum_items(node, prop.identifier) or [getattr(node, prop.identifier)]
            variants.append(property_variant(node, prop.identifier, values))
    for socket in node.inputs:
        if socket.bl_rna.identifier == "NodeSocketMenu":
            if (variant := menu_socket_variant(node, socket)).values:
                variants.append(variant)
    return variants


def states(variants: list[Variant]) -> Iterator[dict[str, Any]]:
    """
    The values to probe a node in, as {variant name: value}; the empty state is the default one. Every combination
    when there are at most `MAX_STATES`, else each variant's values with the others at their defaults and each
    pair's combinations (a socket that only three values together give is then missed).
    """
    yield {}
    total = 1
    for variant in variants:
        total *= len(variant.values)
    if total <= MAX_STATES:
        for combination in itertools.product(*(variant.values for variant in variants)):
            yield {variant.name: value for variant, value in zip(variants, combination)}
        return
    for variant in variants:
        for value in variant.values:
            yield {variant.name: value}
    for variant_a, variant_b in itertools.combinations(variants, 2):
        for value_a, value_b in itertools.product(variant_a.values, variant_b.values):
            yield {variant_a.name: value_a, variant_b.name: value_b}


def enter_state(variants: list[Variant], state: dict[str, Any]) -> bool:
    """
    Set each variant to its value in ``state`` (its default otherwise), and whether the node then has them all. A
    value may only be set once another is (an enum whose items depend on a data type), so this sets them twice. A
    variant the state lacks (a menu socket another value removes) counts only when it is not at its default.
    """
    wanted = {variant.name: state.get(variant.name, variant.default) for variant in variants}
    for _attempt in range(2):
        for variant in variants:
            try:
                variant.write(wanted[variant.name])
            except Exception:
                pass
    for variant in variants:
        try:
            if variant.read() != wanted[variant.name]:
                return False
        except LookupError:
            if wanted[variant.name] != variant.default:
                return False
    return True


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


def probe_node(trees: list[Any], cls: type[bpy.types.Node]) -> tuple[SocketTable, SocketTable]:
    node_id = cls.bl_rna.identifier
    node = add_node(trees, node_id)
    by_identifier = allows_identifier_lookup(node_id)
    collections = item_collections(node)
    for collection in collections:
        collection.clear()
    indexed = not collections and node_id not in DATA_INDEXED_NODES
    inputs, outputs = SocketTable({}, {}), SocketTable({}, {})
    variants = node_variants(node)
    for state in states(variants):
        # A state the node refuses (a value that another one rules out) gives no sockets of its own.
        if not enter_state(variants, state):
            continue
        record_side(inputs, node.inputs, by_identifier=by_identifier, indexed=indexed)
        record_side(outputs, node.outputs, by_identifier=by_identifier, indexed=indexed)
    # An extendable side adds the items a link makes before its virtual socket, shifting the indices after them.
    for table in (inputs, outputs):
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
