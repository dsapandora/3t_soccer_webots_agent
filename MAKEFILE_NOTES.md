# Webots Controllers — Makefile Setup Notes

Referencia de los fixes aplicados a `controllers/soccer/Makefile` y `controllers/soccerv2/Makefile` para que compilen sin necesidad de exportar variables manualmente, especialmente en macOS.

## Problema original

Al correr `make` en `controllers/soccer/` o `controllers/soccerv2/` aparecía:

```
Soccer.cpp:16:10: fatal error: 'RobotisOp2GaitManager.hpp' file not found
   16 | #include <RobotisOp2GaitManager.hpp>
```

A veces también:

```
./Soccer.hpp:23:10: fatal error: 'webots/Robot.hpp' file not found
```

## Causa raíz

El SDK de Webots usa **dos** variables que en macOS apuntan a directorios **distintos**:

| Variable           | Valor correcto en macOS                  | Usada por Webots para...                                              |
|--------------------|------------------------------------------|------------------------------------------------------------------------|
| `WEBOTS_HOME`      | `/Applications/Webots.app`               | Construir paths a headers SDK: `$(WEBOTS_HOME)/Contents/include/...`   |
| `WEBOTS_HOME_PATH` | `/Applications/Webots.app/Contents`      | Cargar subincludes propios: `$(WEBOTS_HOME_PATH)/resources/Makefile.*` |

Si se setea solo `WEBOTS_HOME=/Applications/Webots.app` (lo "estándar" en docs):
- `$(WEBOTS_HOME)/Contents/include/...` → OK
- `$(WEBOTS_HOME_PATH)/resources/Makefile.os.include` → falla (resuelve a `.app/resources/...` que no existe)

Si se setea `WEBOTS_HOME=/Applications/Webots.app/Contents`:
- `$(WEBOTS_HOME_PATH)/resources/Makefile.os.include` → OK
- `$(WEBOTS_HOME)/Contents/include/...` → falla (doble `/Contents/`)

Por eso la única solución es setear ambas con valores diferentes.

Adicionalmente, la ruta `RESOURCES_PATH` (declarada en cada Makefile del controller) en macOS necesita el segmento `/Contents/`:

```
RESOURCES_PATH = $(WEBOTS_HOME)/Contents/projects/robots/robotis/darwin-op
```

mientras que en Linux/Windows sería sin `/Contents/`.

## Fix aplicado a los Makefiles

Ambos `Makefile` ahora tienen un bloque de detección de plataforma con defaults `?=` (no pisan env vars existentes):

```make
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
```

Beneficio: `make` funciona desde terminal sin exportar nada en `~/.zshrc`. Si Webots IDE ya inyecta sus propios `WEBOTS_HOME`/`WEBOTS_HOME_PATH`, los respeta.

## Ubicación real de los headers

Para evitar confusión a futuro: el header `RobotisOp2GaitManager.hpp` (junto a Motion/Vision/Directory managers) vive en

```
$(WEBOTS_HOME)/Contents/projects/robots/robotis/darwin-op/libraries/managers/include/
```

**NO** en `libraries/gait_manager/` (esa estructura aparece en docs viejos pero no se aplica a versiones recientes de Webots).

Listado completo de headers del manager:

```
RobotisOp2DirectoryManager.hpp
RobotisOp2GaitManager.hpp
RobotisOp2MotionManager.hpp
RobotisOp2MotionTimerManager.hpp
RobotisOp2VisionManager.hpp
```

## Estructura del controller `soccerv2/`

Espeja `soccer/` pero con la clase renombrada de `Soccer` a `soccerv2`:

| Archivo                | Rol                                                                  |
|------------------------|----------------------------------------------------------------------|
| `soccerv2.hpp`         | Declaración de la clase `soccerv2` (espejo de `Soccer.hpp`)          |
| `soccerv2.cpp`         | Implementación con los mismos métodos que `Soccer.cpp`               |
| `main.cpp`             | Punto de entrada, instancia `soccerv2` y llama `run()`               |
| `Makefile`             | Build con detección de plataforma (ver fix arriba)                   |
| `Makefile.robotis-op2` | Compilación remota en el robot real ROBOTIS OP2                      |
| `config.ini`           | Parámetros del gait/walking manager (lo lee `RobotisOp2GaitManager`) |
| `runtime.ini`          | Paths Qt para la robot window                                        |

Métodos públicos/privados (idénticos en ambas clases):

- `run()` — feedback loop principal: detecta pelota, camina, patea, se levanta si cae
- `myStep()` — wrapper sobre `step(mTimeStep)` con exit en `-1`
- `wait(int ms)` — espera bloqueante avanzando steps
- `getBallCenter(double &x, double &y)` — detección via `RobotisOp2VisionManager`

## Verificación rápida

Desde cualquiera de los dos directorios:

```bash
make clean && make
```

Salida esperada (extracto):

```
# updating <controller>.d
# updating main.d
# compiling main.cpp (x86_64)
# compiling <controller>.cpp (x86_64)
# linking <controller> (x86_64)
# compiling main.cpp (arm64)
# compiling <controller>.cpp (arm64)
# linking <controller> (arm64)
# creating fat <controller>
# copying to <controller>
```

Si vuelve a aparecer `RobotisOp2GaitManager.hpp file not found`, lo primero a chequear es:

```bash
ls "$WEBOTS_HOME/Contents/projects/robots/robotis/darwin-op/libraries/managers/include/RobotisOp2GaitManager.hpp"
```

Si ese path existe pero `make` falla, probablemente `WEBOTS_HOME_PATH` no esté apuntando a `Contents` — revisar el bloque platform-specific al inicio del `Makefile`.
