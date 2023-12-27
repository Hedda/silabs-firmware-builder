#!/usr/bin/env python3
"""Tool to create a GBL image in a Simplicity Studio build directory."""

from __future__ import annotations

import os
import ast
import pathlib

import sys
import json
import subprocess

from ruamel.yaml import YAML
from xml.etree import ElementTree


def parse_simple_config(file_content: str) -> dict[str, str]:
    """
    Parses a simple key=value file into a dictionary.
    """
    config = {}

    for line in file_content.split("\n"):
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()

    return config


def parse_c_header_defines(file_content: str) -> dict[str, str]:
    """
    Parses a C header file's `#define`s.
    """
    config = {}

    for line in file_content.split("\n"):
        if not line.startswith("#define"):
            continue

        _, key, value = line.split(None, 2)

        try:
            config[key] = ast.literal_eval(value)
        except ValueError:
            pass

    return config


def parse_properties_file(file_content: str) -> dict[str, str | list[str]]:
    """
    Parses custom .properties file format into a dictionary.
    Handles double backslashes as escape characters for spaces.
    """
    properties = {}

    for line in file_content.split("\n"):
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        key, value = line.split("=", 1)
        key = key.strip()

        properties[key] = []
        current_value = ""
        i = 0

        while i < len(value):
            if value[i : i + 2] == "\\\\":
                current_value += " "
                i += 2
            elif value[i] == " ":
                properties[key].append(current_value)
                current_value = ""
                i += 1
            else:
                current_value += value[i]
                i += 1

        if current_value:
            properties[key].append(current_value)

    return properties


def find_file_in_parent_dirs(root: pathlib.Path, filename: str) -> pathlib.Path:
    """
    Finds a file in the given directory or any of its parents.
    """
    root = root.resolve()

    while True:
        if (root / filename).exists():
            return root / filename

        if root.parent == root:
            raise FileNotFoundError(
                f"Could not find {filename} in any parent directory"
            )

        root = root.parent


# We compute all paths based on the build artifact's location
axf_file = pathlib.Path(sys.argv[1]).absolute()
artifact_root = axf_file.parent
project_name = axf_file.stem
slcp_path = find_file_in_parent_dirs(
    root=artifact_root,
    filename=project_name + ".slcp",
)

project_root = slcp_path.parent
slps_path = (project_root / project_name).with_suffix(".slps")

makefile_path = find_file_in_parent_dirs(
    root=artifact_root,
    filename=project_name + ".project.mak",
)

# Extract the Gecko SDK path from the generated Makefile
gsdk_path = pathlib.Path(
    parse_simple_config(makefile_path.read_text())["BASE_SDK_PATH"]
)

# Parse the main Simplicity Studio project config
slcp = YAML(typ="safe").load(slcp_path.read_text())

# Extract the chip ID from the SLPS file
slps_xml = ElementTree.parse(slps_path)
device_part_id = (
    slps_xml.getroot()
    .find(".//properties[@key='projectCommon.partId']")
    .attrib["value"]
    .split(".")[-1]
    .upper()
)
print("Detected device part ID:", device_part_id, flush=True)

gbl_metadata = YAML(typ="safe").load((project_root / "gbl_metadata.yaml").read_text())

# Prepare the GBL metadata
metadata = {
    "metadata_version": 1,
    "sdk_version": slcp["sdk"]["version"],
    "fw_type": gbl_metadata["fw_type"],
    "baudrate": gbl_metadata["baudrate"],
}

# Compute the dynamic metadata
gbl_dynamic = gbl_metadata.get("dynamic", [])

if "ezsp_version" in gbl_dynamic:
    zigbee_esf_props = parse_properties_file(
        (gsdk_path / "protocol/zigbee/esf.properties").read_text()
    )
    metadata["ezsp_version"] = zigbee_esf_props["version"][0]

if "zwave_version" in gbl_dynamic:
    zwave_esf_props = parse_properties_file(
        (gsdk_path / "protocol/z-wave/esf.properties").read_text()
    )
    metadata["zwave_version"] = zwave_esf_props["version"][0]

if "ot_rcp_version" in gbl_dynamic:
    openthread_config_h = parse_c_header_defines(
        (project_root / "config/sl_openthread_generic_config.h").read_text()
    )
    metadata["ot_rcp_version"] = openthread_config_h["PACKAGE_STRING"]

print("Generated GBL metadata:", metadata, flush=True)

# Write it to a file for `commander` to read
(artifact_root / "gbl_metadata.json").write_text(json.dumps(metadata))

# Make sure the Commander binary is included in the PATH on macOS
if sys.platform == "darwin":
    os.environ["PATH"] += (
        os.pathsep
        + "/Applications/Simplicity Studio.app/Contents/Eclipse/developer/adapter_packs/commander/Commander.app/Contents/MacOS"
    )

commander_args = [
    "commander",
    "gbl",
    "create",
    axf_file.with_suffix(".gbl"),
    "--app",
    axf_file,
    "--device",
    device_part_id,
    "--metadata",
    (artifact_root / "gbl_metadata.json"),
]

if gbl_metadata.get("compression", None) is not None:
    commander_args += ["--compress", gbl_metadata["compression"]]

if gbl_metadata.get("sign_key", None) is not None:
    commander_args += ["--sign", gbl_metadata["sign_key"]]

if gbl_metadata.get("encrypt_key", None) is not None:
    commander_args += ["--encrypt", gbl_metadata["encrypt_key"]]

# Finally, generate the GBL
subprocess.run(commander_args, check=True)
