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
#
# Portions of this file are derived from:
#
#   stactools (https://github.com/stac-utils/stactools)
#   Copyright 2020 Azavea (http://www.azavea.com)
#   Apache License, Version 2.0
#
# Significantly modified from the originals:
# - stactools.core.io.xml.XmlElement vendored inline

# ---------------------------------------------------------------------------
# Inline: stactools.core.io.xml.XmlElement (local-file reads only)
# ---------------------------------------------------------------------------
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, cast

from lxml import etree
from lxml.etree import _Element as lxmlElement


class XmlElement:
    def __init__(self, element: lxmlElement) -> None:
        self.element = element

    @lru_cache(maxsize=100)
    def find(self, xpath: str) -> Optional["XmlElement"]:
        node = self.element.find(xpath, self.element.nsmap)
        return None if node is None else XmlElement(node)

    def find_or_throw(
        self, xpath: str, get_exception: Any
    ) -> "XmlElement":
        result = self.find(xpath)
        if result is None:
            raise get_exception(xpath)
        return result

    @lru_cache(maxsize=100)
    def findall(self, xpath: str) -> list["XmlElement"]:
        return [
            XmlElement(e)
            for e in self.element.findall(xpath, self.element.nsmap)
        ]

    @lru_cache(maxsize=100)
    def find_text(self, xpath: str) -> Optional[str]:
        node = self.find(xpath)
        return None if node is None else node.text

    def find_text_or_throw(self, xpath: str, get_exception: Any) -> str:
        result = self.find_text(xpath)
        if result is None:
            raise get_exception(xpath)
        return result

    @lru_cache(maxsize=100)
    def find_attr(self, attr: str, xpath: str) -> Optional[str]:
        node = self.find(xpath)
        return None if node is None else node.get_attr(attr)

    @property
    def text(self) -> Optional[str]:
        if isinstance(self.element.text, str):
            return self.element.text
        elif isinstance(self.element.text, bytes):
            return str(self.element.text, encoding="utf-8")
        else:
            assert self.element.text is None
            return None

    @lru_cache(maxsize=100)
    def get_attr(self, attr: str) -> Optional[str]:
        return cast(Optional[str], self.element.get(attr, None))

    @classmethod
    def from_file(cls, href: str) -> "XmlElement":
        text = Path(href).read_text(encoding="utf-8")
        return cls(etree.fromstring(bytes(text, encoding="utf-8")))

# ---------------------------------------------------------------------------
# ViewingAngle (from stactools-sentinel2 granule_metadata.py)
# ---------------------------------------------------------------------------
@dataclass
class ViewingAngle:
    azimuth: float
    zenith: float

    @classmethod
    def from_nodes(cls, nodes: list[XmlElement]) -> dict[str, "ViewingAngle"]:
        angles: dict[str, ViewingAngle] = {}
        for node in nodes:
            band_id_str = node.get_attr("bandId")
            if band_id_str is None:
                raise ValueError("expected band id on viewing angle node")
            band_id = int(band_id_str)
            if band_id < 8:
                band = f"B0{band_id + 1}"
            elif band_id == 8:
                band = "B8A"
            else:
                band = f"B{band_id:02}"
            zenith = float(
                node.find_text_or_throw(
                    "ZENITH_ANGLE", lambda s: ValueError(f"missing ZENITH_ANGLE: {s}")
                )
            )
            azimuth = float(
                node.find_text_or_throw(
                    "AZIMUTH_ANGLE", lambda s: ValueError(f"missing AZIMUTH_ANGLE: {s}")
                )
            )
            angles[band] = cls(azimuth=azimuth, zenith=zenith)
        return angles
