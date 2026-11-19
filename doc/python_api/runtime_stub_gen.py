# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Generate ``.pyi`` stubs from a running Blender: everything its Python API offers at runtime,
optionally with an add-on enabled and the content of the add-on's ``.blend`` files.

It runs twice in background Blender, with ``$BLENDER_USER_RESOURCES`` naming a folder of its own:

1. ``--write-preferences``, at factory startup: saves preferences with no add-on enabled into that folder, and the
   names of the add-ons factory startup enables (Cycles, glTF, ...) beside them. A user may disable those add-ons, so
   what they register is not a fact of every Blender; and a disabled add-on's classes stay alive, so they are never
   enabled in the second run until its stubs are written.
2. Without ``--factory-startup``, so that Blender loads those preferences, and with ``-Y`` so that the scripts of
   the ``.blend`` files it opens don't run::

      blender --background -Y -noaudio --python doc/python_api/runtime_stub_gen.py -- \
         --output STUBS --work WORK [--addon ADDON_FOLDER] [--python-path SITE_PACKAGES]

   - With ``--addon``, the add-on folder is enabled as a legacy add-on. ``WORK/delta.json`` lists what it adds to
     RNA and ``bpy.ops``.
   - ``sphinx_doc_gen.py`` writes the RST of that state into ``WORK/rst`` and ``sphinx_stub_gen.py`` parses it, as
     for the stubs Blender ships, so the stubs have the same shapes: Type tag views, node socket views and the
     tables of both scripts. Both run in this process. Blender's Python has no ``docutils``: ``--python-path``
     names a folder that has it.
   - On top of that, what only the running Blender has. A class a Python module registers in ``bpy.types``, which
     the docs leave bare (a panel or menu of ``bl_ui`` or of the add-on), is re-exported from its module, with all
     its members. A property typed by a struct the add-on defines (``Scene.my_settings: MySettings``), which the
     docs leave out, is typed by that struct's class, imported from the add-on. The Python source of every module
     the stubs don't render (``bl_ui``, ``bl_operators``, the add-on) is copied beside the stubs.
   - The factory add-ons Blender doesn't keep enabled anyway are then enabled: ``WORK/default_addons.json`` lists
     what they register.
   - With ``--addon``, each ``.blend`` file under the add-on is opened and its local data walked through RNA, into
     ``WORK/blend/<file>.json`` and an overlay stub ``STUBS/<add-on>/_blend/<file>.pyi``.

``WORK/facts.json`` records the build, the timings and what was left out.
"""

__all__ = (
    "main",
)

import argparse
import faulthandler
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import _rna_info  # type: ignore[import-not-found]
import addon_utils  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]

SCRIPT_DIR = Path(__file__).resolve().parent
# The modules whose classes are RNA's own, rendered from RNA (`_bpy_types` defines the Python members of RNA's
# classes, `Object.children`).
RNA_CLASS_MODULES = {"bpy.types", "_bpy_types"}
# What the walk of a `.blend` file skips: the window state, and the back references of every struct.
BLEND_SKIPPED_COLLECTIONS = {"window_managers", "workspaces", "screens", "all_ids"}
BLEND_SKIPPED_POINTERS = {"rna_type", "id_data", "original", "node", "owner"}
BLEND_WALK_DEPTH = 4


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--write-preferences", action="store_true",
        help="Save preferences without add-ons into the user resources folder (the first run)",
    )
    parser.add_argument("--output", type=Path, help="The stubs folder, which must be empty")
    parser.add_argument("--work", type=Path, help="The folder of the RST, facts and manifests")
    parser.add_argument("--addon", type=Path, help="An add-on folder, enabled as a legacy add-on")
    parser.add_argument("--python-path", type=Path, help="A folder with docutils, added to sys.path")
    return parser.parse_args(argv)


# ----------------------------------------------------------------------------
# What enabling or disabling add-ons changes


def rna_facts() -> dict[str, Any]:
    """The structs (their module, base and members) and the operators of the current state."""
    structs, _funcs, ops, _props = _rna_info.BuildRNAInfo(keep_ui=True)
    facts = {
        "structs": {
            struct.identifier: {
                "module": struct.module_name,
                "base": getattr(struct.base, "identifier", None),
                "members": sorted([
                    *(prop.identifier for prop in struct.properties),
                    *("{:s}()".format(func.identifier) for func in struct.functions),
                ]),
            } for struct in structs.values()
        },
        "operators": sorted(op.identifier for op in ops.values()),
    }
    # `BuildRNAInfo` caches its structs by identifier: `sphinx_doc_gen` must read the state it runs in.
    for cls in (_rna_info.InfoStructRNA, _rna_info.InfoPropertyRNA, _rna_info.InfoFunctionRNA,
                _rna_info.InfoOperatorRNA):
        cls.global_lookup.clear()
    return facts


def rna_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What ``after`` has and ``before`` doesn't: structs, members of the structs both have, and operators."""
    old, new = before["structs"], after["structs"]
    return {
        "structs": {key: {"module": value["module"], "base": value["base"]}
                    for key, value in sorted(new.items()) if key not in old},
        "members": {key: added for key, value in sorted(new.items())
                    if key in old and (added := sorted(set(value["members"]) - set(old[key]["members"])))},
        "operators": sorted(set(after["operators"]) - set(before["operators"])),
    }


def default_addons_file() -> Path:
    return Path(bpy.utils.resource_path("USER")) / "runtime_stub_gen_default_addons.json"


def write_preferences() -> None:
    """Save preferences with no add-on enabled, and the names of the add-ons factory startup enables."""
    names = sorted(addon.module for addon in bpy.context.preferences.addons)
    errors: list[str] = []
    for name in names:
        addon_utils.disable(name, default_set=True, handle_error=lambda ex: errors.append(repr(ex)))
    if errors or bpy.context.preferences.addons:
        raise SystemExit("runtime_stub_gen: disabling the factory add-ons failed: {!r}".format(errors))
    bpy.ops.wm.save_userpref()
    default_addons_file().write_text(json.dumps(names), encoding="utf-8")


def enable_default_addons() -> dict[str, dict[str, Any]]:
    """
    Enable the add-ons factory startup enables that are not enabled yet, one at a time: what each registers
    (:func:`rna_delta`), by name. Blender keeps some enabled whatever the preferences say
    (``addon_utils._addons_hidden_core``: ``bl_pkg``, ``io_scene_fbx``, ...): those are in the stubs, as a user
    can't disable them.
    """
    deltas: dict[str, dict[str, Any]] = {}
    for name in json.loads(default_addons_file().read_text(encoding="utf-8")):
        if addon_utils.check(name)[1]:
            continue
        before = rna_facts()
        errors: list[str] = []
        if addon_utils.enable(name, default_set=True, handle_error=lambda ex: errors.append(repr(ex))) is None:
            errors.append("{:s} not enabled".format(name))
        if errors:
            raise SystemExit("runtime_stub_gen: enabling the factory add-on {:s} failed: {!r}".format(name, errors))
        deltas[name] = rna_delta(before, rna_facts())
    return deltas


def enable_addon(addon: Path) -> str:
    """Enable the add-on folder as a legacy add-on (its parent on ``sys.path``); its module name."""
    sys.path.insert(0, str(addon.parent))
    errors: list[str] = []
    module = addon_utils.enable(addon.name, default_set=True, handle_error=lambda ex: errors.append(repr(ex)))
    if module is None or errors:
        raise SystemExit("runtime_stub_gen: enabling {:s} failed: {!r}".format(addon.name, errors))
    return addon.name


# ----------------------------------------------------------------------------
# The stubs: `sphinx_doc_gen.py` and `sphinx_stub_gen.py`, then what only the runtime has


def is_python_module(name: object) -> bool:
    """Whether ``name`` is a module imported from Python source."""
    return isinstance(name, str) and (getattr(sys.modules.get(name), "__file__", None) or "").endswith(".py")


def bare_python_classes(types_module: Any) -> dict[str, str]:
    """
    The classes of ``bpy.types`` a Python module defines (not RNA, not ``_bpy_types``) that ``sphinx_doc_gen``
    writes bare (the menus, panels and headers of ``BARE_SUBCLASS_BASES``) and that the module has under that name:
    name -> module. The others keep the members it writes: typed by their docstrings, which the module's source may
    not annotate, or bare for a class the module makes at runtime (``type(...)`` in ``bl_ui.node_add_menu``).
    """
    found: dict[str, str] = {}
    for cls in types_module.classes:
        value = getattr(bpy.types, cls.name, None)
        module = getattr(value, "__module__", None)
        if (
                not cls.attributes and not cls.methods and isinstance(value, type) and hasattr(value, "bl_rna") and
                isinstance(module, str) and module not in RNA_CLASS_MODULES and is_python_module(module) and
                getattr(sys.modules[module], cls.name, None) is value
        ):
            found[cls.name] = module
    return found


def python_operator_classes(types_module: Any) -> dict[str, str]:
    """
    The operator classes a Python module registers, which ``bpy.types`` has under their class name
    (``bpy.types.PREFERENCES_OT_copy_prev``) but ``sphinx_doc_gen`` doesn't write: name -> module, for those the
    module has under that name.
    """
    documented = {cls.name for cls in types_module.classes}
    found: dict[str, str] = {}
    pending: list[type] = [bpy.types.Operator, bpy.types.Macro]
    while pending:
        for cls in pending.pop().__subclasses__():
            pending.append(cls)
            name, module = cls.__name__, getattr(cls, "__module__", None)
            if (
                    name not in documented and isinstance(module, str) and module not in RNA_CLASS_MODULES and
                    is_python_module(module) and getattr(sys.modules[module], name, None) is cls and
                    getattr(bpy.types, name, None) is cls
            ):
                found[name] = module
    return found


def type_addon_struct_properties(doc_gen: Any, stub_gen: Any, types_module: Any, package: str) -> list[str]:
    """
    Add the properties typed by a struct the add-on ``package`` defines (``Scene.my_settings: MySettings``), which
    ``sphinx_doc_gen`` leaves out, typed by the struct's class imported from its module; their names
    (``Scene.my_settings``). A collection a Python script registers is a ``bpy_prop_collection_idprop``, which can
    add and remove items.
    """
    structs, _funcs, _ops, _props = doc_gen.rna_info_BuildRNAInfo_cache()
    addon_structs = {
        struct.identifier: struct.module_name for struct in structs.values()
        if struct.module_name == package or struct.module_name.startswith(package + ".")
    }
    classes = {cls.name: cls for cls in types_module.classes}
    added = []
    for struct in sorted(structs.values(), key=lambda struct: struct.identifier):
        if (cls := classes.get(struct.identifier)) is None:
            continue
        for prop in struct.properties:
            ids = {getattr(prop.fixed_type, "identifier", None), getattr(prop.srna, "identifier", None)}
            if not ids & addon_structs.keys():
                continue
            type_descr, _type_info = prop.get_type_description(
                class_fmt=":class:`{:s}`",
                mathutils_fmt=":class:`mathutils.{:s}`",
                literal_fmt="``{!r}``",
                collection_id=(
                    ":class:`{:s}`".format(doc_gen._BPY_PROP_COLLECTION_IDPROP_PYCAPI)
                    if prop.bl_prop.is_runtime else doc_gen._BPY_PROP_COLLECTION_ID
                ),
            )
            for struct_id in ids & addon_structs.keys():
                type_descr = type_descr.replace(
                    ":class:`{:s}`".format(struct_id),
                    ":class:`{:s}.{:s}`".format(addon_structs[struct_id], struct_id),
                )
            # As docutils reads it: `:class:`bpy_prop_collection`\ [X]` escapes the space before `[`.
            cls.attributes.append(stub_gen.AttrInfo(
                name=prop.identifier, type_str=type_descr.replace("\\ ", ""), readonly=prop.is_readonly))
            added.append("{:s}.{:s}".format(struct.identifier, prop.identifier))
    return added


def copy_python_modules(output: Path, package_roots: list[Path]) -> int:
    """
    Copy the Python source the runtime offers into ``output``, beside the rendered stubs: every ``.py`` of the
    module folders (``scripts/modules``, ``scripts/startup``, Freestyle's) and of the packages in ``package_roots``
    (an enabled add-on). A module the stubs render (``bpy.utils``, ``bpy_extras``) keeps its stub. The number of
    files copied.
    """
    scripts = Path(bpy.utils.resource_path("LOCAL")) / "scripts"
    sources = [
        (folder, sorted(folder.rglob("*.py")))
        for folder in (scripts / "modules", scripts / "startup", scripts / "freestyle" / "modules")
        if folder.is_dir()
    ]
    sources += [(root.parent, sorted(root.rglob("*.py"))) for root in package_roots]
    copied = 0
    for folder, files in sources:
        for path in files:
            relative = path.relative_to(folder)
            if "__pycache__" in relative.parts:
                continue
            target = output / relative
            stub = target.with_name("__init__.pyi") if target.name == "__init__.py" else target.with_suffix(".pyi")
            if stub.exists() or (target.with_suffix("") / "__init__.pyi").exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied += 1
    return copied


def generate_stubs(doc_gen: Any, stub_gen: Any, output: Path, work: Path, package: str | None) -> dict:
    """Write the stubs of the current state, with the add-on ``package`` enabled, into ``output``; the facts of the
    run."""
    started = time.perf_counter()
    rst = work / "rst"
    if doc_gen.main(["--output", str(rst)]) not in {0, None}:
        raise SystemExit("runtime_stub_gen: sphinx_doc_gen.py failed")
    facts: dict[str, Any] = {"seconds_rst": round(time.perf_counter() - started, 2)}

    stub_gen.setup_docutils_once()
    modules = stub_gen.collect_modules(rst / "sphinx-in")
    types_module = modules["bpy.types"]
    reexported = bare_python_classes(types_module)
    types_module.classes = [cls for cls in types_module.classes if cls.name not in reexported]
    reexported.update(python_operator_classes(types_module))
    facts["typed_addon_properties"] = (
        type_addon_struct_properties(doc_gen, stub_gen, types_module, package) if package else []
    )

    contents, ref_errors = stub_gen.render_stubs(modules)
    # A class of a Python module resolves once its source is copied beside the stubs.
    facts["type_ref_errors"] = sorted(
        "{:s} in {:s}".format(error.ref, error.module) for error in ref_errors
        if not is_python_module(error.ref.rpartition(".")[0])
    )
    reexports = "".join(
        "from {:s} import {:s} as {:s}\n".format(module, name, name) for name, module in sorted(reexported.items())
    )
    contents = [(name, content + reexports if name == "bpy.types" else content) for name, content in contents]
    stub_gen.write_stubs(output, contents)
    facts["rendered_modules"] = len(contents)
    facts["reexported_classes"] = reexported
    # The add-on, and the packages outside the module folders that re-exported classes come from (`bl_pkg`).
    package_files = [
        getattr(sys.modules.get(root), "__file__", None)
        for root in sorted({package or "", *(module.partition(".")[0] for module in reexported.values())})
    ]
    package_roots = [Path(file).parent for file in package_files if file and file.endswith("__init__.py")]
    facts["copied_python_files"] = copy_python_modules(output, package_roots)
    facts["seconds_stubs"] = round(time.perf_counter() - started, 2)
    return facts


# ----------------------------------------------------------------------------
# `.blend` content: a generic RNA walk of every local ID


def walk_value(value: Any, depth: int, *, top: bool = False) -> dict[str, Any] | None:
    """
    What a manifest records about an RNA value: a struct's class and its pointers and named collections, to
    ``depth``; an ID other than the one walked (``top``) as a reference by class and name.
    """
    if depth <= 0 or value is None:
        return None
    if isinstance(value, bpy.types.ID) and not top:
        return {"id": "{:s}:{:s}".format(type(value).bl_rna.identifier, value.name)}
    if not isinstance(value, bpy.types.bpy_struct) or type(value).__module__ not in RNA_CLASS_MODULES:
        # Not RNA's own struct: an add-on's settings (`Object.cycles`), which the stubs don't declare.
        return None
    out: dict[str, Any] = {"class": type(value).bl_rna.identifier}
    for prop in value.bl_rna.properties:
        if prop.identifier in BLEND_SKIPPED_POINTERS or prop.type not in {"POINTER", "COLLECTION"}:
            continue
        try:
            item = getattr(value, prop.identifier)
        except Exception:
            continue
        if prop.type == "COLLECTION":
            name_prop = getattr(prop.fixed_type, "name_property", None)
            if name_prop is None or not len(item):
                continue
            # Sockets are looked up by identifier first (`rna_NodeInputs_lookup_string`).
            key = "identifier" if prop.fixed_type.identifier.startswith("NodeSocket") else name_prop.identifier
            out[prop.identifier] = collection_entry(prop, {
                getattr(element, key, "") or "": (
                    walk_value(element, depth - 1) or {"class": type(element).bl_rna.identifier}
                ) for element in item
            })
        elif (sub := walk_value(item, depth - 1)) is not None:
            out[prop.identifier] = sub
    return out


def collection_entry(prop: Any, items: dict[str, Any]) -> dict[str, Any]:
    """A walked collection: its wrapper struct (``ObjectModifiers``), its item struct and the items by key."""
    return {"wrapper": getattr(prop.srna, "identifier", None), "item": prop.fixed_type.identifier, "items": items}


def walk_blend(path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    content: dict[str, Any] = {}
    for prop in bpy.data.bl_rna.properties:
        if prop.type != "COLLECTION" or prop.identifier in BLEND_SKIPPED_COLLECTIONS:
            continue
        local = [id_data for id_data in getattr(bpy.data, prop.identifier) if id_data.library is None]
        if not local:
            continue
        content[prop.identifier] = collection_entry(prop, {
            id_data.name: {
                **(walk_value(id_data, BLEND_WALK_DEPTH, top=True) or {}),
                "custom_properties": {key: type(value).__name__ for key, value in id_data.items()},
            } for id_data in local
        })
    return {"file": path.name, "seconds": round(time.perf_counter() - started, 3), "data": content}


def blend_overlay(manifest: dict[str, Any]) -> str:
    """
    An overlay stub for one ``.blend`` file: ``data``, a ``BlendData`` whose collections take the file's names as
    ``Literal`` keys and give each item's class, down to what the walk recorded (modifiers, material slots, nodes,
    sockets) and the class of each ID pointer. A script binds it under ``TYPE_CHECKING``
    (``from my_addon._blend.assets import data``).
    """
    classes: list[str] = []
    imports: set[str] = {"BlendData", "bpy_prop_collection"}

    def ident(text: str) -> str:
        return re.sub(r"\W", "_", text)

    def struct_class(path: str, node: dict[str, Any]) -> str:
        """The class of a walked struct: a subclass of its class, narrowing what the walk recorded."""
        base = node["class"]
        imports.add(base)
        body = []
        for prop, sub in node.items():
            if not isinstance(sub, dict) or prop == "custom_properties":
                continue
            if "items" in sub:
                result = collection_class("{:s}_{:s}".format(path, ident(prop)), sub)
            elif "id" in sub:
                result = sub["id"].split(":", 1)[0]
                imports.add(result)
            else:
                result = struct_class("{:s}_{:s}".format(path, ident(prop)), sub)
            body.append("    @property\n    def {:s}(self) -> {:s}: ...\n".format(prop, result))
        if not body:
            return base
        classes.append("class _{:s}({:s}):\n{:s}".format(path, base, "".join(body)))
        return "_" + path

    def collection_class(path: str, entry: dict[str, Any]) -> str:
        item = entry["item"]
        imports.add(item)
        wrapper = entry["wrapper"]
        if wrapper:
            imports.add(wrapper)
        body = []
        for key, value in entry["items"].items():
            result = struct_class("{:s}_{:s}".format(path, ident(key)), value) if "class" in value else item
            body.append("    @overload\n    def __getitem__(self, key: Literal[{!r}], /) -> {:s}: ...\n".format(
                key, result))
        body += [
            "    @overload\n    def __getitem__(self, key: int | str, /) -> {:s}: ...\n".format(item),
            "    @overload\n    def __getitem__(self, key: slice, /) -> list[{:s}]: ...\n".format(item),
        ]
        base = wrapper or "bpy_prop_collection[{:s}]".format(item)
        classes.append("class _{:s}({:s}):\n{:s}".format(path, base, "".join(body)))
        return "_" + path

    data_body = [
        "    @property\n    def {:s}(self) -> {:s}: ...\n".format(
            collection, collection_class(ident(collection), entry))
        for collection, entry in manifest["data"].items()
    ]
    classes.append("class _BlendData(BlendData):\n{:s}\n".format("".join(data_body) or "    ..."))
    header = (
        '"""The content of {:s} as Blender loaded it, generated by runtime_stub_gen.py."""\n\n'
        "from typing import Literal, overload\n\n"
        "from bpy.types import {:s}\n\n"
    ).format(manifest["file"], ", ".join(sorted(imports)))
    return header + "\n".join(classes) + "\ndata: _BlendData\n"


def write_blend_content(addon: Path, package: str, output: Path, work: Path) -> list[str]:
    """Walk each ``.blend`` file under the add-on into a manifest and an overlay stub; their paths."""
    blends = sorted(addon.rglob("*.blend"))
    for blend in blends:
        manifest = walk_blend(blend)
        stem = blend.relative_to(addon).with_suffix("").as_posix().replace("/", "__")
        (work / "blend").mkdir(exist_ok=True)
        (work / "blend" / "{:s}.json".format(stem)).write_text(
            json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
        overlay_dir = output / package / "_blend"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        (overlay_dir / "{:s}.pyi".format(stem)).write_text(blend_overlay(manifest), encoding="utf-8")
    return [blend.relative_to(addon).as_posix() for blend in blends]


# ----------------------------------------------------------------------------


def main() -> None:
    # A crash in Blender (a freed RNA struct read) then names the Python line that read it.
    faulthandler.enable()
    args = parse_args()
    if args.write_preferences:
        write_preferences()
        return
    if args.output is None or args.work is None:
        raise SystemExit("runtime_stub_gen: --output and --work are required")
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit("runtime_stub_gen: the output folder is not empty: {:s}".format(str(args.output)))
    if enabled := sorted(addon.module for addon in bpy.context.preferences.addons):
        raise SystemExit(
            "runtime_stub_gen: add-ons are enabled ({:s}): run with the preferences --write-preferences saved, "
            "without --factory-startup".format(", ".join(enabled)))
    args.work.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(SCRIPT_DIR), *([str(args.python_path)] if args.python_path else [])]
    import sphinx_doc_gen  # type: ignore[import-not-found]
    import sphinx_stub_gen  # type: ignore[import-not-found]

    started = time.perf_counter()
    facts: dict[str, Any] = {
        "blender": bpy.app.version_string,
        "build_hash": bpy.app.build_hash.decode(),
        "platform": sys.platform,
        "build_options": sorted(name for name in dir(bpy.app.build_options)
                                if not name.startswith("_") and getattr(bpy.app.build_options, name) is True),
    }
    package = None
    if args.addon is not None:
        before = rna_facts()
        package = enable_addon(args.addon.resolve())
        (args.work / "delta.json").write_text(json.dumps(
            {"package": package, **rna_delta(before, rna_facts()),
             "modules": sorted(name for name in sys.modules if name == package or name.startswith(package + "."))},
            indent=1), encoding="utf-8")

    facts.update(generate_stubs(sphinx_doc_gen, sphinx_stub_gen, args.output, args.work, package))

    default_addons = enable_default_addons()
    facts["default_addons"] = sorted(default_addons)
    (args.work / "default_addons.json").write_text(json.dumps(default_addons, indent=1), encoding="utf-8")

    if package is not None:
        facts["blend_files"] = write_blend_content(args.addon.resolve(), package, args.output, args.work)
    facts["seconds_total"] = round(time.perf_counter() - started, 2)
    (args.work / "facts.json").write_text(json.dumps(facts, indent=1, sort_keys=True), encoding="utf-8")
    print("runtime_stub_gen: done in {:.1f}s".format(facts["seconds_total"]))
    # A crash while Blender exits, past this point, is not this script's.
    faulthandler.disable()


if __name__ == "__main__":
    main()
