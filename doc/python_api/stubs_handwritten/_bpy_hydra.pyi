# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# `_bpy_hydra`: the private C module behind `bpy.types.HydraRenderEngine`.
# It has no API docs, so it is typed partially: every member is `Any`.

from typing import Any

def __getattr__(name: str) -> Any: ...
