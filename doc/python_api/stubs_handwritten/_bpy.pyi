# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# `_bpy`: the private C module behind `bpy` (`bpy.types`, `bpy.ops`, `bpy.utils`, `bpy.props`, ...).
# It has no API docs, so it is typed partially: every member is `Any`.

from typing import Any

def __getattr__(name: str) -> Any: ...
