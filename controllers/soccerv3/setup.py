"""Build the soccerv3_native pybind11 extension.

Usage:
    python -m pip install pybind11
    python setup.py build_ext --inplace

Produces a .so/.dylib next to this file, importable as `soccerv3_native`.
"""

from __future__ import annotations

import os
import platform
import sys

from setuptools import Extension, setup


def _webots_home() -> str:
    home = os.environ.get("WEBOTS_HOME")
    if home:
        return home
    if platform.system() == "Darwin":
        return "/Applications/Webots.app"
    if os.name == "nt":
        return "C:/Program Files/Webots"
    return "/usr/local/webots"


def _platform_paths(webots_home: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (include_dirs, library_dirs, libraries, extra_link_args)."""
    if platform.system() == "Darwin":
        contents = os.path.join(webots_home, "Contents")
        webots_include = os.path.join(contents, "include", "controller", "cpp")
        webots_lib = os.path.join(contents, "lib", "controller")
        resources = os.path.join(contents, "projects", "robots", "robotis", "darwin-op")
    else:
        webots_include = os.path.join(webots_home, "include", "controller", "cpp")
        webots_lib = os.path.join(webots_home, "lib", "controller")
        resources = os.path.join(webots_home, "projects", "robots", "robotis", "darwin-op")

    managers_include = os.path.join(resources, "libraries", "managers", "include")
    framework_include = os.path.join(
        resources, "libraries", "robotis-op2", "robotis", "Framework", "include"
    )
    managers_lib = os.path.join(resources, "libraries", "managers")
    robotis_lib = os.path.join(resources, "libraries", "robotis-op2")

    include_dirs = [webots_include, managers_include, framework_include]
    library_dirs = [webots_lib, managers_lib, robotis_lib]
    libraries = ["managers", "robotis-op2", "CppController"]

    # Embed runtime search paths so dlopen can resolve the Webots dylibs.
    # Webots ships its dylibs with relative install_names like:
    #   @rpath/projects/robots/robotis/darwin-op/libraries/managers/libmanagers.dylib
    #   @rpath/Contents/lib/controller/libCppController.dylib
    # so the rpath must be the Webots root directories, NOT the lib folders.
    if platform.system() == "Darwin":
        extra_link_args = [
            f"-Wl,-rpath,{contents}",     # resolves managers + robotis-op2
            f"-Wl,-rpath,{webots_home}",  # resolves Contents/lib/controller/libCppController.dylib
        ]
    elif platform.system() == "Linux":
        extra_link_args = [
            f"-Wl,-rpath,{webots_home}",
            f"-Wl,-rpath,{managers_lib}",
            f"-Wl,-rpath,{robotis_lib}",
            f"-Wl,-rpath,{webots_lib}",
        ]
    else:
        extra_link_args = []

    return include_dirs, library_dirs, libraries, extra_link_args


def _pybind11_include() -> str:
    try:
        import pybind11
    except ImportError:
        sys.stderr.write(
            "ERROR: pybind11 not installed. Run: pip install pybind11\n"
        )
        sys.exit(1)
    return pybind11.get_include()


WEBOTS_HOME = _webots_home()
include_dirs, library_dirs, libraries, extra_link_args = _platform_paths(WEBOTS_HOME)
include_dirs.append(_pybind11_include())

extra_compile_args = ["-std=c++17", "-O2", "-Wall"]

ext = Extension(
    name="soccerv3_native",
    sources=["soccerv3_native.cpp"],
    include_dirs=include_dirs,
    library_dirs=library_dirs,
    libraries=libraries,
    extra_compile_args=extra_compile_args,
    extra_link_args=extra_link_args,
    language="c++",
)

setup(
    name="soccerv3_native",
    version="0.1.0",
    description="pybind11 wrapper for Webots ROBOTIS-OP2 managers",
    ext_modules=[ext],
)
