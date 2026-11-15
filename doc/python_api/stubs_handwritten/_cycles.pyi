# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# `_cycles`: the private C module of Cycles' render engine (the Cycles add-on calls it).
# It has no API docs, so it is typed partially: every member is `Any`.

from typing import Any

def __getattr__(name: str) -> Any: ...
