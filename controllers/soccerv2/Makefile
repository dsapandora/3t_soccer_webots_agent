# Copyright 1996-2024 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

### Generic Makefile.include for Webots controllers, physics plugins, robot
### window libraries, remote control libraries and other libraries
### to be used with GNU make
###
### Platforms: Windows, macOS, Linux
### Languages: C, C++
###
### Authors: Olivier Michel, Yvan Bourquin, Fabien Rohrer
###          Edmund Ronald, Sergei Poskriakov
###
###-----------------------------------------------------------------------------
###
### This file is meant to be included from the Makefile files located in the
### Webots projects subdirectories. It is possible to set a number of variables
### to customize the build process, i.e., add source files, compilation flags,
### include paths, libraries, etc. These variables should be set in your local
### Makefile just before including this Makefile.include. This Makefile.include
### should never be modified.
###
### Here is a description of the variables you may set in your local Makefile:
###
### ---- C Sources ----
### if your program uses several C source files:
### C_SOURCES = my_plugin.c my_clever_algo.c my_graphics.c
###
### ---- C++ Sources ----
### if your program uses several C++ source files:
### CXX_SOURCES = my_plugin.cc my_clever_algo.cpp my_graphics.c++
###
### ---- Compilation options ----
### if special compilation flags are necessary:
### CFLAGS = -Wno-unused-result
###
### ---- Linked libraries ----
### if your program needs additional libraries:
### INCLUDE = -I"/my_library_path/include"
### LIBRARIES = -L"/path/to/my/library" -lmy_library -lmy_other_library
###
### ---- Linking options ----
### if special linking flags are needed:
### LFLAGS = -s
###
### ---- Webots included libraries ----
### if you want to use the Webots C API in your C++ controller program:
### USE_C_API = true
###
### ---- Debug mode ----
### if you want to display the gcc command line for compilation and link, as
### well as the rm command details used for cleaning:
### VERBOSE = 1
###
###-----------------------------------------------------------------------------

null :=
space := $(null) $(null)

# Default WEBOTS_HOME per platform if not exported in the environment.
# On macOS the convention is WEBOTS_HOME=/Applications/Webots.app, while the
# Webots Makefile.include itself reads its own resources via $(WEBOTS_HOME_PATH)
# which must point at the Contents directory.
ifeq ($(OS),Windows_NT)
  WEBOTS_HOME ?= C:/Program Files/Webots
  RESOURCES_PATH = $(WEBOTS_HOME)/projects/robots/robotis/darwin-op
  WEBOTS_HOME_PATH ?= $(subst $(space),\ ,$(strip $(subst \,/,$(WEBOTS_HOME))))
else
  UNAME_S := $(shell uname -s)
  ifeq ($(UNAME_S),Darwin)
    WEBOTS_HOME ?= /Applications/Webots.app
    RESOURCES_PATH = $(WEBOTS_HOME)/Contents/projects/robots/robotis/darwin-op
    WEBOTS_HOME_PATH ?= $(WEBOTS_HOME)/Contents
  else
    WEBOTS_HOME ?= /usr/local/webots
    RESOURCES_PATH = $(WEBOTS_HOME)/projects/robots/robotis/darwin-op
    WEBOTS_HOME_PATH ?= $(subst $(space),\ ,$(strip $(subst \,/,$(WEBOTS_HOME))))
  endif
endif

INCLUDE = -I"$(RESOURCES_PATH)/libraries/managers/include" -I"$(RESOURCES_PATH)/libraries/robotis-op2/robotis/Framework/include"
LIBRARIES = -L"$(RESOURCES_PATH)/libraries/robotis-op2" -lrobotis-op2 -L"$(RESOURCES_PATH)/libraries/managers" -lmanagers
CXX_SOURCES = $(wildcard *.cpp)

### Do not modify: this includes Webots global Makefile.include
include $(WEBOTS_HOME_PATH)/resources/Makefile.include
