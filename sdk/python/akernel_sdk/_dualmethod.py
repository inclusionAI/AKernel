# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Typed dispatch for E2B-style instance and by-ID class operations."""

from __future__ import annotations

from typing import Any, Generic, TypeVar, cast, overload

InstanceCall = TypeVar("InstanceCall")
ClassCall = TypeVar("ClassCall")


class DualMethod(Generic[InstanceCall, ClassCall]):
    def __init__(self, instance_method: Any, class_method: str) -> None:
        self._instance_method = instance_method
        self._class_method = class_method
        self.__doc__ = instance_method.__doc__

    @overload
    def __get__(self, instance: None, owner: type) -> ClassCall: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> InstanceCall: ...

    def __get__(
        self, instance: object | None, owner: type | None = None
    ) -> InstanceCall | ClassCall:
        if instance is None:
            return cast(ClassCall, getattr(owner, self._class_method))
        return cast(InstanceCall, self._instance_method.__get__(instance, owner))
