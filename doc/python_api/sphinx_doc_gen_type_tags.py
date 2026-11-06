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


class TypeTags(NamedTuple):
    # Struct identifier -> {tag attribute: the values of its instances}, for the subclasses of a base.
    values: dict[str, dict[str, list[str]]]
    # The tag values the probe could not make an instance of: `"Strip.type 'MOVIE': error"`.
    missing: list[str]


class Probe:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.ids: list[Any] = []
        self.missing: list[str] = []
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


def enum_ids(struct_id: str, attr: str) -> list[str]:
    return [item.identifier for item in getattr(bpy.types, struct_id).bl_rna.properties[attr].enum_items]


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
    yield "NodeSocket", "bl_idname", sockets
    yield "NodeSocket", "type", sockets


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
    data_families,
    strip_families,
    space_families,
    node_families,
    property_families,
)


# -----------------------------------------------------------------------------
# Probe

def record(values: dict[str, dict[str, list[str]]], family: Family) -> None:
    base, attr, instances = family
    for instance in instances:
        struct_id = type(instance).bl_rna.identifier
        if struct_id == base or base not in struct_ancestors(struct_id):
            continue
        struct_values = values.setdefault(struct_id, {}).setdefault(attr, [])
        if (value := getattr(instance, attr)) not in struct_values:
            struct_values.append(value)


def probe() -> TypeTags:
    """Make the instances of every family and read their subclasses and tags."""
    tags = TypeTags(values={}, missing=[])
    with tempfile.TemporaryDirectory() as directory:
        p = Probe(directory)
        try:
            for families in FAMILIES:
                for family in families(p):
                    record(tags.values, family)
        finally:
            bpy.data.batch_remove([id_data for id_data in p.ids if id_data is not None])
        tags.missing.extend(p.missing)
    for struct_values in tags.values.values():
        for values in struct_values.values():
            values.sort()
    return tags
