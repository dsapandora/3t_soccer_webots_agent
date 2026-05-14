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

# Webots Makefile system
#
# You may add some variable definitions hereafter to customize the build process
# See documentation in $(WEBOTS_HOME_PATH)/resources/Makefile.include

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
