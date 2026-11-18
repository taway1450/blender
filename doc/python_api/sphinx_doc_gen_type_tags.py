# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Type tags of the refined structs, for the stubs (see `USE_STUB_GEN` in `sphinx_doc_gen.py`).

A struct such as :class:`bpy.types.Modifier` is refined to a subclass by a C function (`rna_Modifier_refine`),
which RNA introspection cannot read. This module makes an instance of each value of a struct's tag
(``Modifier.type``, ``Space.type``, ...) inside Blender and reads which subclass Blender gives it,
so that the stubs can state that a :class:`bpy.types.SubsurfModifier` has the type ``'SUBSURF'``.
"""

__all__ = (
    "BaseTag",
    "Constructor",
    "TypeTags",
    "probe",
)

import os
import tempfile
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple

import bpy

# Name of every data-block the probe creates.
NAME = "type_tag_probe"

# A base struct, its tag attribute and instances of its subclasses.
Family = tuple[str, str, list[Any]]

# A base struct without a tag and instances of each of its subclasses.
UntaggedFamily = tuple[str, list[Any]]


class TagView(NamedTuple):
    """
    What a tag value implies for the other attributes of a struct that is not refined:
    ``_Object_MESH``, an :class:`bpy.types.Object` whose ``type`` is ``'MESH'``, has a :class:`bpy.types.Mesh`
    as its ``data``.
    """
    identifier: str
    base: str
    attr: str
    value: str
    # `(attribute, the struct identifier of its value or None when it is always None, whether it can be None)`.
    members: list[tuple[str, str | None, bool]]


class BaseTag(NamedTuple):
    """A tag that each subclass of a base defines itself (`LineStyleColorModifier.type`), so the base has it too."""
    values: list[str]
    is_readonly: bool


class Constructor(NamedTuple):
    """
    A function whose argument names the subclass of the struct it returns: ``BlendDataCurves.new(name, type)``
    gives a :class:`bpy.types.TextCurve` for ``'FONT'``, though :class:`bpy.types.Curve` has no tag that says so.
    """
    struct: str
    function: str
    parameter: str
    # Each value of the parameter -> the struct identifier of the result.
    results: dict[str, str]


class TypeTags(NamedTuple):
    # Struct identifier -> {tag attribute: the values of its instances}, for the subclasses of a base.
    # A struct between the base and its subclasses holds a value its instances all share
    # (`NodeTreeInterfaceSocket.item_type` is ``'SOCKET'``).
    values: dict[str, dict[str, list[str]]]
    # The tag views of the structs whose tag decides the type of other attributes.
    views: list[TagView]
    # Base struct identifier -> the one subclass its tag allows, so every instance is that subclass.
    refined: dict[str, str]
    # Base struct identifier -> {tag attribute only its subclasses define: every value}
    # (`LineStyleColorModifier.type`, which each color modifier defines itself).
    base_tags: dict[str, dict[str, BaseTag]]
    # Base struct identifier without a tag -> the subclasses its instances are always one of
    # (a `GreasePencilTreeNode` is a `GreasePencilLayer` or a `GreasePencilLayerGroup`).
    unions: dict[str, list[str]]
    # The functions whose argument names the subclass of their result.
    constructors: list[Constructor]
    # The tag values the probe could not make an instance of: `"Strip.type 'MOVIE': error"`.
    missing: list[str]


class Probe:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.ids: list[Any] = []
        self.missing: list[str] = []
        # Undo what removing `ids` does not: called last first.
        self.cleanups: list[Callable[[], None]] = []
        self._channel = 0

    def keep(self, id_data: Any) -> Any:
        """Remember a new data-block, to remove it afterwards."""
        self.ids.append(id_data)
        return id_data

    def channel(self) -> int:
        """A new sequencer channel."""
        self._channel += 1
        return self._channel

    def family(self, base: str, attr: str, make: Callable[[str], Any]) -> Family:
        """An instance for each value of the enum ``base.attr``, made by ``make(value)``."""
        instances = []
        for value in enum_ids(base, attr):
            try:
                instance = make(value)
            except Exception as ex:
                self.missing.append("{:s}.{:s} {!r}: {:s}".format(base, attr, value, str(ex).strip()))
                continue
            if instance is None:
                self.missing.append("{:s}.{:s} {!r}: no instance made".format(base, attr, value))
                continue
            instances.append(instance)
        return base, attr, instances

    def found(self, base: str, attr: str, instances: list[Any]) -> Family:
        """The instances of ``base`` that Blender's data already holds, noting the values none of them has."""
        values = {getattr(instance, attr) for instance in instances}
        for value in enum_ids(base, attr):
            if value not in values:
                self.missing.append("{:s}.{:s} {!r}: no instance found".format(base, attr, value))
        return base, attr, instances


def tag_property(struct_id: str, attr: str) -> Any:
    """
    The property ``attr`` of ``struct_id``, or of its subclasses when each defines it
    (every ``LineStyleColorModifier`` subclass defines its own ``type``).
    """
    if (prop := getattr(bpy.types, struct_id).bl_rna.properties.get(attr)) is not None:
        return prop
    for name in subclasses(getattr(bpy.types, struct_id)):
        if (prop := getattr(bpy.types, name).bl_rna.properties.get(attr)) is not None:
            return prop
    raise KeyError("{:s}.{:s}".format(struct_id, attr))


def enum_ids(struct_id: str, attr: str) -> list[str]:
    return [item.identifier for item in tag_property(struct_id, attr).enum_items]


def first(*makers: Callable[[], Any]) -> Any:
    """The result of the first maker that gives one (some ``new()`` functions give None on failure)."""
    error: Exception | None = None
    for make in makers:
        try:
            if (result := make()) is not None:
                return result
        except Exception as ex:
            error = ex
    if error is not None:
        raise error
    return None


def struct_ancestors(struct_id: str) -> list[str]:
    """``struct_id`` and its bases, nearest first."""
    result = []
    srna = getattr(bpy.types, struct_id).bl_rna
    while srna is not None:
        result.append(srna.identifier)
        srna = srna.base
    return result


def subclasses(base: type) -> list[str]:
    """The identifiers of the structs in ``bpy.types`` deriving from ``base``."""
    return [
        name for name in dir(bpy.types)
        if isinstance(cls := getattr(bpy.types, name), type) and issubclass(cls, base) and cls is not base
    ]


# -----------------------------------------------------------------------------
# Families

def new_object(p: Probe, data: Any) -> Any:
    return p.keep(bpy.data.objects.new(NAME, p.keep(data)))


def mesh_object(p: Probe) -> Any:
    return new_object(p, bpy.data.meshes.new(NAME))


def object_families(p: Probe) -> Iterator[Family]:
    mesh_ob = mesh_object(p)
    gp_ob = new_object(p, bpy.data.grease_pencils.new(NAME))
    volume_ob = new_object(p, bpy.data.volumes.new(NAME))
    yield p.family("Modifier", "type", lambda value: first(
        lambda: mesh_ob.modifiers.new(NAME, value),
        lambda: gp_ob.modifiers.new(NAME, value),
        lambda: volume_ob.modifiers.new(NAME, value),
    ))
    yield p.family("ShaderFx", "type", lambda value: gp_ob.shader_effects.new(NAME, value))
    yield p.family("Constraint", "type", lambda value: mesh_ob.constraints.new(value))
    fcurve = mesh_ob.driver_add("location", 0)
    yield p.family("FModifier", "type", lambda value: fcurve.modifiers.new(value))


def boid_families(p: Probe) -> Iterator[Family]:
    # Only an operator adds a boid rule, to the active state of the particle settings in the context.
    settings = p.keep(bpy.data.particles.new(NAME))
    settings.physics_type = 'BOIDS'
    rules = settings.boids.active_boid_state.rules

    def make(value: str) -> Any:
        with bpy.context.temp_override(particle_settings=settings):
            bpy.ops.boid.rule_add(type=value)
        return rules[-1]
    yield p.family("BoidRule", "type", make)


def linestyle_families(p: Probe) -> Iterator[Family]:
    linestyle = p.keep(bpy.data.linestyles.new(NAME))
    for base, collection in (
            ("LineStyleColorModifier", linestyle.color_modifiers),
            ("LineStyleAlphaModifier", linestyle.alpha_modifiers),
            ("LineStyleThicknessModifier", linestyle.thickness_modifiers),
            ("LineStyleGeometryModifier", linestyle.geometry_modifiers),
    ):
        yield p.family(base, "type", lambda value, collection=collection: collection.new(NAME, value))


def project_families(p: Probe) -> Iterator[Family]:
    # Project variables exist only in a project: make one when there is none.
    if bpy.data.project is None:
        try:
            bpy.ops.project.new(directory=os.path.join(p.directory, NAME))
        except Exception as ex:
            p.missing.append("ProjectVariable.type: no project: {:s}".format(str(ex).strip()))
            return
    variables = bpy.data.project.variables
    made: list[Any] = []

    def make(value: str) -> Any:
        made.append(variable := variables.new("{:s}_{:s}".format(NAME, value.lower()), type=value))
        return variable

    def cleanup() -> None:
        for variable in reversed(made):
            variables.remove(variable)
        # Clear the project's unsaved changes, so that Blender does not write them when it quits.
        bpy.ops.project.config_save()
    p.cleanups.append(cleanup)
    yield p.family("ProjectVariable", "type", make)


def data_families(p: Probe) -> Iterator[Family]:
    yield p.family("Light", "type", lambda value: p.keep(bpy.data.lights.new(NAME, value)))
    yield p.family("LightProbe", "type", lambda value: p.keep(bpy.data.lightprobes.new(NAME, value)))
    yield p.family("Texture", "type", lambda value: p.keep(bpy.data.textures.new(NAME, value)))
    mesh = p.keep(bpy.data.meshes.new(NAME))
    yield p.family("Attribute", "data_type", lambda value: mesh.attributes.new(NAME + value, value, 'POINT'))
    yield p.family(
        "ActionStrip", "type",
        lambda value: p.keep(bpy.data.actions.new(NAME)).layers.new(NAME).strips.new(type=value),
    )


def sample_files(p: Probe) -> tuple[str, str]:
    """A PNG image and a WAV sound, for the strips that load a file."""
    import wave
    image_path = os.path.join(p.directory, NAME + ".png")
    image = p.keep(bpy.data.images.new(NAME, 4, 4))
    image.filepath_raw = image_path
    image.file_format = 'PNG'
    image.save()
    sound_path = os.path.join(p.directory, NAME + ".wav")
    with wave.open(sound_path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(b"\0\0" * 800)
    return image_path, sound_path


def strip_families(p: Probe) -> Iterator[Family]:
    image_path, sound_path = sample_files(p)
    scene = p.keep(bpy.data.scenes.new(NAME))
    other_scene = p.keep(bpy.data.scenes.new(NAME))
    collection = scene.sequence_editor_create().strips
    inputs = [collection.new_effect(NAME, 'COLOR', p.channel(), 1, length=10) for _ in range(2)]

    def effect(value: str, n_inputs: int) -> Any:
        return collection.new_effect(
            NAME, value, p.channel(), 1, length=10,
            **{"input{:d}".format(i + 1): inputs[i] for i in range(n_inputs)},
        )

    makers = {
        'SCENE': lambda: collection.new_scene(NAME, other_scene, p.channel(), 1),
        'IMAGE': lambda: collection.new_image(NAME, image_path, p.channel(), 1),
        'MOVIE': lambda: collection.new_movie(NAME, image_path, p.channel(), 1),
        'SOUND': lambda: collection.new_sound(NAME, sound_path, p.channel(), 1),
        'MOVIECLIP': lambda: collection.new_clip(
            NAME, p.keep(bpy.data.movieclips.load(image_path)), p.channel(), 1),
        'MASK': lambda: collection.new_mask(NAME, p.keep(bpy.data.masks.new(NAME)), p.channel(), 1),
        'META': lambda: collection.new_meta(NAME, p.channel(), 1),
    }
    yield p.family("Strip", "type", lambda value: makers[value]() if value in makers else first(
        lambda: effect(value, 0),
        lambda: effect(value, 1),
        lambda: effect(value, 2),
    ))
    yield p.family("StripModifier", "type", lambda value: inputs[0].modifiers.new(NAME, value))


def space_families(p: Probe) -> Iterator[Family]:
    # Only an area of a window's screen gets a new space when its type changes (in `--background` as well).
    windows = bpy.context.window_manager.windows
    if not windows:
        p.missing.append("Space.type: no window")
        return
    area = windows[0].screen.areas[0]
    area_type = area.type

    def make(value: str) -> Any:
        area.type = value
        return area.spaces.active
    try:
        yield p.family("Space", "type", make)
    finally:
        area.type = area_type


def node_families(p: Probe) -> Iterator[Family]:
    # The built-in node trees, every node that one of them accepts, and their sockets.
    trees = []
    for tree_id in subclasses(bpy.types.NodeTree):
        try:
            trees.append(p.keep(bpy.data.node_groups.new(NAME, tree_id)))
        except Exception:
            pass
    nodes = []
    for node_id in subclasses(bpy.types.Node):
        for tree in trees:
            try:
                nodes.append(tree.nodes.new(node_id))
                break
            except Exception:
                pass
    sockets = [socket for node in nodes for socket in (*node.inputs, *node.outputs)]
    yield "NodeTree", "bl_idname", trees
    yield "Node", "bl_idname", nodes
    # The legacy type, which several node classes share (`'MATH'`).
    yield "Node", "type", nodes
    yield "NodeSocket", "bl_idname", sockets
    yield "NodeSocket", "type", sockets
    # A panel and a socket of each type in the interface of each tree.
    items = []
    for tree in trees:
        try:
            items.append(tree.interface.new_panel(NAME))
        except Exception:
            pass
        for socket_id in subclasses(bpy.types.NodeSocket):
            try:
                items.append(tree.interface.new_socket(NAME, socket_type=socket_id, in_out='INPUT'))
            except Exception:
                pass
    yield "NodeTreeInterfaceItem", "item_type", items


def editor_families(p: Probe) -> Iterator[Family]:
    # Only drawing a spreadsheet makes its table and viewer path: read those of the startup file's spreadsheets.
    spreadsheets = [
        space for screen in bpy.data.screens for area in screen.areas for space in area.spaces
        if space.type == 'SPREADSHEET'
    ]
    yield p.found("SpreadsheetTableID", "type", [
        table.id for space in spreadsheets for table in space.tables if table.id is not None
    ])
    yield p.found("ViewerPathElem", "type", [elem for space in spreadsheets for elem in space.viewer_path.path])


def property_families(_p: Probe) -> Iterator[Family]:
    # The properties of every struct and the parameters of its functions.
    properties = []
    for name in dir(bpy.types):
        srna = getattr(getattr(bpy.types, name), "bl_rna", None)
        if srna is None:
            continue
        properties.extend(srna.properties)
        for function in srna.functions:
            properties.extend(function.parameters)
    yield "Property", "type", properties


FAMILIES: tuple[Callable[[Probe], Iterator[Family]], ...] = (
    object_families,
    boid_families,
    linestyle_families,
    project_families,
    data_families,
    strip_families,
    space_families,
    node_families,
    editor_families,
    property_families,
)


def grease_pencil_families(p: Probe) -> Iterator[UntaggedFamily]:
    grease_pencil = p.keep(bpy.data.grease_pencils.new(NAME))
    grease_pencil.layers.new(NAME)
    grease_pencil.layer_groups.new(NAME)
    yield "GreasePencilTreeNode", list(grease_pencil.root_nodes)


UNTAGGED_FAMILIES: tuple[Callable[[Probe], Iterator[UntaggedFamily]], ...] = (
    grease_pencil_families,
)


# -----------------------------------------------------------------------------
# Constructors: a function, its parameter, and the result for each value of the parameter.

ConstructorFamily = tuple[str, str, str, dict[str, Any]]


def curve_constructors(p: Probe) -> Iterator[ConstructorFamily]:
    results = {}
    for value in function_enum_ids("BlendDataCurves", "new", "type"):
        try:
            results[value] = p.keep(bpy.data.curves.new(NAME, value))
        except Exception as ex:
            p.missing.append("BlendDataCurves.new(type={!r}): {:s}".format(value, str(ex).strip()))
    yield "BlendDataCurves", "new", "type", results


def function_enum_ids(struct_id: str, function: str, parameter: str) -> list[str]:
    params = getattr(bpy.types, struct_id).bl_rna.functions[function].parameters
    return [item.identifier for item in params[parameter].enum_items]


CONSTRUCTOR_FAMILIES: tuple[Callable[[Probe], Iterator[ConstructorFamily]], ...] = (
    curve_constructors,
)


# -----------------------------------------------------------------------------
# Tag views: a base struct, its tag attribute, the attributes the tag decides,
# and instances of the base for each tag value.

ViewFamily = tuple[str, str, tuple[str, ...], dict[str, list[Any]]]


def object_views(p: Probe) -> Iterator[ViewFamily]:
    # Objects of each type, with every variant of their data, as the data may be refined itself (`Light`).
    data = bpy.data

    def armature_assigned() -> Any:
        ob = new_object(p, data.armatures.new(NAME))
        ob.data = p.keep(data.armatures.new(NAME))
        return ob

    def empty_image() -> Any:
        # An empty can display an image, its data.
        ob = new_object(p, None)
        ob.data = p.keep(data.images.new(NAME, 4, 4))
        return ob

    makers: dict[str, list[Callable[[], Any]]] = {
        'MESH': [lambda: new_object(p, data.meshes.new(NAME))],
        'CURVE': [lambda: new_object(p, data.curves.new(NAME, 'CURVE'))],
        'SURFACE': [lambda: new_object(p, data.curves.new(NAME, 'SURFACE'))],
        'FONT': [lambda: new_object(p, data.curves.new(NAME, 'FONT'))],
        'META': [lambda: new_object(p, data.metaballs.new(NAME))],
        'CURVES': [lambda: new_object(p, data.hair_curves.new(NAME))],
        'POINTCLOUD': [lambda: new_object(p, data.pointclouds.new(NAME))],
        'VOLUME': [lambda: new_object(p, data.volumes.new(NAME))],
        'GREASEPENCIL': [lambda: new_object(p, data.grease_pencils.new(NAME))],
        # The pose is None until the depsgraph builds it, while assigning an armature builds it at once.
        'ARMATURE': [lambda: new_object(p, data.armatures.new(NAME)), armature_assigned],
        'LATTICE': [lambda: new_object(p, data.lattices.new(NAME))],
        'CAMERA': [lambda: new_object(p, data.cameras.new(NAME))],
        'SPEAKER': [lambda: new_object(p, data.speakers.new(NAME))],
        'LIGHT': [
            lambda value=value: new_object(p, data.lights.new(NAME, value)) for value in enum_ids("Light", "type")
        ],
        'LIGHT_PROBE': [
            lambda value=value: new_object(p, data.lightprobes.new(NAME, value))
            for value in enum_ids("LightProbe", "type")
        ],
        'EMPTY': [lambda: new_object(p, None), empty_image],
    }
    instances: dict[str, list[Any]] = {}
    for value in enum_ids("Object", "type"):
        if value not in makers:
            p.missing.append("Object.type {!r}: no data to make an object of".format(value))
            continue
        try:
            instances[value] = [make() for make in makers[value]]
        except Exception as ex:
            p.missing.append("Object.type {!r}: {:s}".format(value, str(ex).strip()))
    yield "Object", "type", ("data", "pose"), instances


def fluid_views(p: Probe) -> Iterator[ViewFamily]:
    # The settings pointer of each fluid type (one fluid modifier per object).
    instances: dict[str, list[Any]] = {}
    for value in enum_ids("FluidModifier", "fluid_type"):
        md = mesh_object(p).modifiers.new(NAME, 'FLUID')
        md.fluid_type = value
        instances[value] = [md]
    yield "FluidModifier", "fluid_type", ("domain_settings", "effector_settings", "flow_settings"), instances


VIEW_FAMILIES: tuple[Callable[[Probe], Iterator[ViewFamily]], ...] = (
    object_views,
    fluid_views,
)


# -----------------------------------------------------------------------------
# Probe

def record(values: dict[str, dict[str, list[str]]], family: Family) -> None:
    base, attr, instances = family
    # Tag value -> the subclasses of the instances that have it.
    by_value: dict[str, set[str]] = {}
    for instance in instances:
        struct_id = type(instance).bl_rna.identifier
        if struct_id == base or base not in struct_ancestors(struct_id):
            continue
        by_value.setdefault(getattr(instance, attr), set()).add(struct_id)
    for value, struct_ids in by_value.items():
        # When the subclasses of a value derive from a struct below the base whose instances all have the value,
        # that struct holds it (`NodeTreeInterfaceSocket` for ``'SOCKET'``, not each socket type).
        if len(struct_ids) > 1 and (holder := common_base(struct_ids)) != base and all(
                other == value or holder not in struct_ancestors(struct_id)
                for other, other_ids in by_value.items() for struct_id in other_ids
        ):
            struct_ids = {holder}
        for struct_id in struct_ids:
            struct_values = values.setdefault(struct_id, {}).setdefault(attr, [])
            if value not in struct_values:
                struct_values.append(value)


def record_base_tags(base_tags: dict[str, dict[str, BaseTag]], family: Family) -> None:
    """A tag that only the subclasses of the base define: the base has it too, with every value."""
    base, attr, _instances = family
    if attr not in getattr(bpy.types, base).bl_rna.properties:
        prop = tag_property(base, attr)
        base_tags.setdefault(base, {})[attr] = BaseTag(enum_ids(base, attr), prop.is_readonly)


def record_union(unions: dict[str, list[str]], missing: list[str], family: UntaggedFamily) -> None:
    """The instances of a base without a tag are of one of its subclasses, when the probe made all of them."""
    base, instances = family
    struct_ids = {type(instance).bl_rna.identifier for instance in instances}
    expected = {
        struct_id for struct_id in subclasses(getattr(bpy.types, base))
        if getattr(bpy.types, struct_id).bl_rna.base.identifier == base
    }
    if base in struct_ids or struct_ids != expected:
        missing.append("{:s}: instances of {:s}, subclasses {:s}".format(
            base, ", ".join(sorted(struct_ids)), ", ".join(sorted(expected))))
        return
    unions[base] = sorted(struct_ids)


def record_constructor(constructors: list[Constructor], family: ConstructorFamily) -> None:
    struct_id, function, parameter, results = family
    result_ids = {value: type(result).bl_rna.identifier for value, result in results.items()}
    if len(set(result_ids.values())) > 1:
        constructors.append(Constructor(struct_id, function, parameter, result_ids))


def record_refined(refined: dict[str, str], family: Family) -> None:
    """When the base's tag is an enum with a single value, its instances are all of one subclass."""
    base, attr, instances = family
    prop = tag_property(base, attr)
    if prop.type != 'ENUM' or len(prop.enum_items) != 1 or not instances:
        return
    struct_ids = {type(instance).bl_rna.identifier for instance in instances}
    if len(struct_ids) == 1 and (struct_id := struct_ids.pop()) != base:
        refined[base] = struct_id


def common_base(struct_ids: set[str]) -> str:
    """The nearest struct all of ``struct_ids`` derive from."""
    ancestors = [struct_ancestors(struct_id) for struct_id in sorted(struct_ids)]
    return next(struct_id for struct_id in ancestors[0] if all(struct_id in other for other in ancestors[1:]))


def record_views(views: list[TagView], missing: list[str], family: ViewFamily) -> None:
    base, attr, member_ids, instances = family
    for value, value_instances in instances.items():
        if any(getattr(instance, attr) != value for instance in value_instances):
            missing.append("{:s}.{:s} {!r}: the instances have another value".format(base, attr, value))
            continue
        members = []
        for member_id in member_ids:
            member_values = [getattr(instance, member_id) for instance in value_instances]
            struct_ids = {type(member).bl_rna.identifier for member in member_values if member is not None}
            members.append((
                member_id,
                # The nearest common base of the variants (`Light` for the data of a light object).
                common_base(struct_ids) if struct_ids else None,
                any(member is None for member in member_values),
            ))
        views.append(TagView("_{:s}_{:s}".format(base, value), base, attr, value, members))


def probe() -> TypeTags:
    """Make the instances of every family and read their subclasses and tags."""
    tags = TypeTags(values={}, views=[], refined={}, base_tags={}, unions={}, constructors=[], missing=[])
    with tempfile.TemporaryDirectory() as directory:
        p = Probe(directory)
        try:
            for families in FAMILIES:
                for family in families(p):
                    record(tags.values, family)
                    record_refined(tags.refined, family)
                    record_base_tags(tags.base_tags, family)
            for untagged_families in UNTAGGED_FAMILIES:
                for untagged_family in untagged_families(p):
                    record_union(tags.unions, p.missing, untagged_family)
            for constructor_families in CONSTRUCTOR_FAMILIES:
                for constructor_family in constructor_families(p):
                    record_constructor(tags.constructors, constructor_family)
            for view_families in VIEW_FAMILIES:
                for view_family in view_families(p):
                    record_views(tags.views, p.missing, view_family)
        finally:
            for cleanup in reversed(p.cleanups):
                cleanup()
            bpy.data.batch_remove([id_data for id_data in p.ids if id_data is not None])
        tags.missing.extend(p.missing)
    for struct_values in tags.values.values():
        for values in struct_values.values():
            values.sort()
    return tags
