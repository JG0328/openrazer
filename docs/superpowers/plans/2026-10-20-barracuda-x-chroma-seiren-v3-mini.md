# Barracuda X Chroma y Seiren V3 Mini (uso local): plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objetivo:** que el daemon de OpenRazer de este repo publique tres dispositivos sin driver de
kernel: el dongle del Barracuda X Chroma `1532:0574`, con batería, carga, color fijo, apagado,
brillo y tiempo de apagado automático; el headset por cable `1532:0573`; y el Seiren V3 Mini
`1532:056a`. Polychromatic 0.9.8, sin modificar, debe mostrarlos.

**Arquitectura:**
- Una familia de clases de dispositivo en el daemon que se descubren por el subsistema `hid`
  existente (sin `device_type` en sysfs).
- El dongle se habla por `/dev/hidrawN`, con un transporte genérico (hilo lector y peticiones
  con espera) y un módulo de protocolo puro (tramas, CRC y una puerta de seguridad).
- El headset responde a eventos: el estado se guarda en caché y los getters de D-Bus nunca
  hacen E/S.
- En la máquina de José, un drop-in de systemd de usuario cambia el daemon empaquetado por el
  del repo.

**Tecnología:** Python 3.14, dbus-python, PyGObject, pyudev y `unittest` (no hay pytest). Bus
D-Bus privado con `dbus-broker-launch` para la prueba de extremo a extremo.

**Spec:** `docs/superpowers/specs/2026-10-20-barracuda-x-chroma-seiren-v3-mini-design.md`.
Léelo antes de empezar cualquier tarea: la sección 3 fija el protocolo y la 8 las decisiones.

## Global Constraints

- Solo biblioteca estándar y las dependencias que el daemon ya usa. No instalar paquetes.
  Pruebas con `unittest`.
- Comando de pruebas (desde la raíz del repo):
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=pylib:daemon:daemon/tests DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/bus python3 -m unittest -v <módulo>`.
  - La suite completa es `... python3 -m unittest discover -s daemon/tests -t daemon/tests -v`.
  - Línea base: 24 pruebas, 1 fallo que ya existía (`test_notify_run_effect_edge_case_3`).
    Ninguna tarea puede añadir fallos.
- **Al dispositivo solo se escriben tramas que pasen `barracuda_protocol.check_frame`.**
  Nunca se escribe:
  - el report 6/7 (página 0xFF13, canal RACE);
  - ningún report Feature, ni ioctl;
  - el output report 5.
- Al menos 50 ms entre escrituras; respuestas con un timeout de 1,5 s.
- Los getters de D-Bus (`getBattery`, `isCharging`, `getIdleTime`, `getBrightness`,
  `getDeviceName`, `getFirmware` y `getDeviceImage`):
  - **nunca hacen E/S y nunca lanzan excepciones**;
  - `getDeviceImage` devuelve siempre un `str`.
- Ningún constructor, `match()`, `_suspend_device`, `_resume_device` ni setter puede propagar
  excepciones de E/S.
- Nombres exactos:

  | PID | Nombre | Tipo |
  |---|---|---|
  | `0x0574` | `Razer Barracuda X Chroma (Wireless)` | `headset` |
  | `0x0573` | `Razer Barracuda X Chroma (Wired)` | `headset` |
  | `0x056A` | `Razer Seiren V3 Mini` | `accessory` |

- Serial: `"HIDRAW%04X%04X" % (USB_VID, USB_PID)`, por ejemplo `HIDRAW15320574`.
- Batería: `float` entero de 0 a 100, o `-1.0` si nunca se conoció. `isCharging` es `False` si
  no se sabe o si el enlace está caído.
- Brillo: el valor de D-Bus (0–100) se convierte con `int(round(b * 255 / 100))`. Se envía
  **solo** cuando José lo cambia; nunca al arrancar, reanudar o reconectar.
- Apagado automático: D-Bus en segundos y headset en minutos, con
  `minutos = min(60, max(0, round(segundos / 60)))`.
- "Apagado" de luces y bloqueo de pantalla = trama de color negra (`build_color(0, 0, 0)`),
  nunca brillo 0.
- No modificar Polychromatic, `python3-openrazer` ni los archivos empaquetados. Ningún daemon
  del repo en el bus de sesión real antes de la Tarea 6.
- Commits que terminan con `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **El headset está apagado o en Bluetooth al arrancar el daemon.** El dispositivo aparece al
   instante: `getBattery` = −1.0, `isCharging` = False, sin errores ni bloqueos de arranque.
   Prueba: Tarea 4, `test_construct_with_silent_dongle_is_fast_and_safe`.
2. **El dongle se desconecta o el lector recibe EIO.** Los setters de D-Bus no lanzan y la
   persistencia se actualiza igual. Prueba: Tarea 4, `test_setters_after_disconnect_do_not_raise`.
3. **Falta la regla udev** (abrir el nodo da EACCES). El dispositivo aparece sin batería y el
   daemon sigue. Prueba: Tarea 4, `test_open_failure_still_publishes_device`.
4. **La pantalla se bloquea con el headset apagado y después se enciende.** Al enlazar se envía
   negro, no el color; al desbloquear vuelve el color. Prueba: Tarea 4,
   `test_link_up_while_suspended_sends_black`.
5. **La sincronización de efectos trae un `setBrightness` de otro dispositivo**
   (`sync_effects_enabled = True` en la configuración de José). No se escribe SET 0xC1. Prueba:
   Tarea 4, `test_synced_brightness_is_ignored`.

---

### Task 1: Módulo de protocolo (tramas, CRC, puerta de seguridad)

**Files:**
- Create: `daemon/openrazer_daemon/misc/barracuda_protocol.py`
- Test: `daemon/tests/test_barracuda_protocol.py`

**Interfaces:**
- Produce las constantes `REPORT_ID = 0x02`, `FRAME_LEN = 64`, `LED_COUNT = 6`,
  `PROP_LINK = 0x20`, `PROP_BATTERY = 0x21`, `PROP_CHARGING = 0x2A`,
  `PROP_IDLE_MINUTES = 0x2C`, `PROP_BRIGHTNESS = 0x41`, `STATUS_OK = 0x02` y
  `STATUS_EVENT = 0x0A`.
- `build_get(prop: int) -> bytes`
- `build_set(prop: int, value: int) -> bytes`: `prop` es el id de GET (0x41 o 0x2C) y la trama
  lleva `prop | 0x80`.
- `build_color(red: int, green: int, blue: int) -> bytes`: los 6 LEDs del mismo color.
- `check_frame(frame: bytes) -> Optional[str]`: `None` si se puede escribir; si no, el motivo.
- `class Report(NamedTuple)`: `kind: str` (`'reply'` o `'event'`), `prop: int`, `status: int`,
  `value: int`.
- `parse_report(data: bytes) -> Optional[Report]`
- `reply_matcher(prop: int) -> Callable[[bytes], bool]`: acepta solo respuestas
  (`kind == 'reply'`) de esa propiedad.

Sobre del host (spec §3): `[0]=02 [1]=00 [2]=60 [3..5]=00 [6]=SZ [7]=clase [8]=id [9]=80
[10..]=args [62]=XOR(0..61) [63]=00`.
- GET: args `[P,00,00]`, SZ 04.
- SET: args `[P|0x80,00,01,v]`, SZ 05.
- Color: clase/id `0F/03`, args `[00,00,00,00,05]` seguidos de 6×RGB, SZ `0x17`.

`check_frame` admite exactamente:
- GET de {0x20, 0x21, 0x2A, 0x2C, 0x41};
- SET de {0xC1, 0xAC} con N = 1;
- la trama de color.

Exige además los bytes fijos del sobre, el CRC, `[63]=00` y ceros después de la carga útil.

`parse_report` exige:
- `len == 64`, `[0] == 0x02`, `[2] == 0x60`, `[8] == 0x80`, `[9] == 0x80`;
- `[62] == XOR(bytes 1..61)`.

El tipo es `'event'` si `[1] == 0x0A` y `'reply'` en cualquier otro caso, con
`status = [1]`, `prop = [10]` y `value = [13]`. Todo lo demás devuelve `None`, incluidos los
reports de 2 bytes de los botones (`0c 01`).

- [ ] **Step 1: Escribir las pruebas que fallan**

Helper de prueba: `frame(prefix_hex, crc)` = 64 ceros con `bytes.fromhex(prefix_hex)` al inicio
y `crc` en el byte 62. Vectores, todos verificados contra tramas reales de esta unidad:

```python
def test_build_get_vectors(self):
    self.assertEqual(build_get(0x21), frame("0200600000000400008021", 0xC7))
    for prop, crc in ((0x20, 0xC6), (0x2A, 0xCC), (0x2C, 0xCA), (0x41, 0xA7)):
        self.assertEqual(build_get(prop)[62], crc)

def test_build_set_vectors(self):
    self.assertEqual(build_set(0x41, 0xFF), frame("02006000000005000080c10001ff", 0xD8))
    for prop, value, crc in ((0x41, 0x40, 0x67), (0x41, 0x80, 0xA7), (0x2C, 7, 0x4D), (0x2C, 0, 0x4A), (0x2C, 60, 0x76)):
        self.assertEqual(build_set(prop, value)[62], crc)

def test_build_color_layout(self):
    f = build_color(255, 0, 0)
    self.assertEqual(f[:15], bytes.fromhex("020060000000170f03800000000005"))
    self.assertEqual(f[15:33], bytes([255, 0, 0]) * 6)
    self.assertEqual(f[33:62], bytes(29)); self.assertEqual(f[62], 0xFC); self.assertEqual(f[63], 0)

def test_check_frame_accepts_builders(self):   # every builder output above -> None
def test_check_frame_rejects(self):
    # report ID 6; len 63; GET 0x40; SET 0xC0; SET 0x99; wrong CRC; byte9 = 0x00;
    # class 0F/02; a GET with a stray non-zero byte at index 20; byte 63 = 1 -> each returns a str

def test_parse_reply_and_event(self):
    self.assertEqual(parse_report(frame("020260000000050080802101014f", 0x09)),
                     Report('reply', 0x21, 0x02, 0x4F))
    self.assertEqual(parse_report(frame("020a600000000500808020020101", 0x4D)),
                     Report('event', 0x20, 0x0A, 0x01))
    self.assertEqual(parse_report(frame("020a60520000050080802102015e", 0x41)),
                     Report('event', 0x21, 0x0A, 0x5E))

def test_parse_rejects(self):
    # bad CRC (0x09 -> 0x08) -> None; b"\x0c\x01" -> None; 63 bytes -> None; byte8 = 0x00 -> None

def test_reply_matcher(self):
    # matcher(0x21) accepts the 0x21 reply above, rejects the 0x21 event and the 0x20 event
```

- [ ] **Step 2: Comprobar que fallan.** Ejecuta `test_barracuda_protocol` con el comando de las
  Global Constraints. Esperado: `ModuleNotFoundError: openrazer_daemon.misc.barracuda_protocol`.
- [ ] **Step 3: Implementar el módulo.** Funciones puras; los valores fuera de 0..255 lanzan
  `ValueError`. `check_frame` comprueba el CRC antes que la clase/id.
- [ ] **Step 4: Comprobar que pasan.** El mismo comando debe dar OK con 8 pruebas.
- [ ] **Step 5: Commit**: `git add` de los dos archivos, mensaje
  `daemon: Add Barracuda X Chroma property protocol helpers`.

### Task 2: Transporte hidraw genérico

**Files:**
- Create: `daemon/openrazer_daemon/misc/hidraw_transport.py`
- Test: `daemon/tests/test_hidraw_transport.py`

**Interfaces:**
- Consume: nada de tareas anteriores. El validador se inyecta y en la Tarea 4 será
  `barracuda_protocol.check_frame`.
- Produce:
  - `open_hidraw(path: str) -> int`: `os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)`.
  - `class HidrawTransport`:
    - `__init__(self, fd: int, on_report: Callable[[bytes], None], on_disconnect: Callable[[], None], validator: Callable[[bytes], Optional[str]], logger: logging.Logger, write_gap: float = 0.05)`.
      Es dueño del `fd` y lo cierra.
    - `start(self) -> None`: arranca el hilo lector `hidraw-reader` (`daemon=True`).
    - `write(self, frame: bytes) -> bool`: devuelve `False` si el validador la rechaza (lo
      registra en el log), si el transporte está cerrado o muerto, o si hay `OSError`. Respeta
      `write_gap` y serializa con un lock.
    - `request(self, frame: bytes, matcher: Callable[[bytes], bool], timeout: float = 1.5) -> Optional[bytes]`:
      registra el waiter **antes** de escribir y devuelve el primer report que acepte
      `matcher`, o `None` si vence el timeout o hay desconexión.
    - `close(self) -> None`: idempotente; para el hilo y cierra el fd.
    - `alive` (property, `bool`).

El hilo lector hace `select` con un timeout de 0,2 s para ver la señal de parada y lee con
`os.read(fd, 1024)`. Cada report va al primer waiter cuyo `matcher` lo acepte; si ninguno lo
acepta, va a `on_report`. Las excepciones del callback se registran y no matan el hilo.

Se trata como desconexión: `b''`, o `OSError` con errno EIO, ENODEV, EPIPE, ECONNRESET o
EBADF. En ese caso `alive` pasa a `False`, se despierta a los waiters con `None` y se llama
`on_disconnect()` **una sola vez**.

- [ ] **Step 1: Escribir las pruebas que fallan.** Usa
  `host, dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)` y pasa
  `host.detach()` como `fd`. Pruebas:
  - `test_write_sends_exact_frame`: `dev.recv(4096)` es la trama.
  - `test_validator_refusal_returns_false_and_sends_nothing`: `dev` en modo no bloqueante;
    `recv` lanza `BlockingIOError`.
  - `test_request_returns_matching_reply`: un hilo hace de dongle, primero envía un report que
    no coincide y luego el que coincide. El primero llega a `on_report`; `request` devuelve el
    segundo.
  - `test_request_timeout_returns_none`: con `timeout=0.1`, el tiempo transcurrido está entre
    0,1 s y 0,5 s.
  - `test_unsolicited_report_goes_to_on_report`.
  - `test_peer_close_calls_on_disconnect_once`: `dev.close()`; en menos de 1 s, una sola
    llamada, `alive` es `False` y `write` devuelve `False`.
  - `test_read_eio_is_disconnect`: `mock.patch('openrazer_daemon.misc.hidraw_transport.os.read', side_effect=OSError(errno.EIO, 'EIO'))`.
  - `test_write_gap_is_respected`: dos `write` seguidos llegan con ≥ 0,05 s de diferencia.
  - `test_close_is_idempotent`: dos `close()`; el hilo termina en menos de 1 s.
- [ ] **Step 2: Comprobar que fallan.** `test_hidraw_transport` con el comando global; esperado
  `ModuleNotFoundError`.
- [ ] **Step 3: Implementar `HidrawTransport` y `open_hidraw`** como se describe arriba.
- [ ] **Step 4: Comprobar que pasan.** OK, 9 pruebas, sin hilos colgados (el proceso termina).
- [ ] **Step 5: Commit**: `daemon: Add generic hidraw transport`.

### Task 3: Descubrimiento sin driver de kernel y los dos dispositivos pasivos

**Files:**
- Modify: `daemon/openrazer_daemon/hardware/device_base.py`
  - añadir `import grp`;
  - en `RazerDevice`, el atributo de clase `HIDRAW_DEVICE = False` y el classmethod
    `check_access`.
- Modify: `daemon/openrazer_daemon/daemon.py:486-493`: sustituir el bloque "Checking
  permissions" por la llamada al hook. Mismo mensaje `critical` y mismo `break` cuando devuelve
  `False`.
- Modify: `daemon/openrazer_daemon/daemon.py:526`: en el bucle de clases de `_add_device`, hacer
  `continue` si `self._test_dir is not None and device_class.HIDRAW_DEVICE`.
- Create: `daemon/openrazer_daemon/hardware/hidraw_devices.py`
- Create: `daemon/tests/hidraw_fixtures.py` (helpers; no coincide con `test*.py`)
- Test: `daemon/tests/test_hidraw_devices.py`

**Interfaces:**
- `RazerDevice.check_access(cls, dev_path: str, testing: bool) -> bool` (classmethod). Por
  defecto reproduce exactamente la lógica actual: stat de `device_type`, comparar el gid con
  `os.getgid()` o el grupo `plugdev`.
- En `hidraw_devices.py`, la base se importa con alias:
  `from openrazer_daemon.hardware.device_base import RazerDevice as _RazerDevice`. La clase base
  lleva **guion bajo** para que `get_device_classes()` la ignore.
- `class _RazerHidrawDevice(_RazerDevice)`:
  - Atributos: `USB_VID = 0x1532`, `HIDRAW_DEVICE = True`, `OPENS_HIDRAW = False`,
    `DEVICE_IMAGE = ''` y `HIDRAW_DEVICE_NAME = ''` (cada subclase lo define).
  - `match(cls, device_id: str, dev_path: str) -> bool` (classmethod) devuelve `True` si se
    cumplen las tres condiciones:
    - `re.fullmatch(r'0003:%04X:%04X\.[0-9A-F]{4}' % (cls.USB_VID, cls.USB_PID), device_id)`;
    - en `dev_path` no hay `device_type`;
    - existe `dev_path/hidraw/` con al menos una entrada.

    Cualquier `OSError` da `False`.
  - `hidraw_node(dev_path: str) -> Optional[str]` (staticmethod): `'/dev/' +` la primera
    entrada ordenada de `dev_path/hidraw/`, o `None`.
  - `check_access(cls, dev_path, testing) -> bool`: `True` si `testing` o si no
    `cls.OPENS_HIDRAW`; si no, `os.access(node, os.R_OK | os.W_OK)`.
  - `get_serial(self) -> str`: el formato de las Global Constraints. Sin E/S ni esperas.
  - `load_methods(self) -> None`: llama a `super().load_methods()` y vuelve a registrar con
    `self.add_dbus_method(iface, name, fn, in_sig, out_sig)`:
    - `('razer.device.misc', 'getDeviceName', self._dbus_get_device_name, None, 's')`, que
      devuelve `HIDRAW_DEVICE_NAME`;
    - `('razer.device.misc', 'getFirmware', self._dbus_get_firmware, None, 's')`, que devuelve
      `'unknown'`;
    - y después cada tupla de `self._extra_dbus_methods()`.
  - `_extra_dbus_methods(self) -> list`: devuelve `[]`. Es el hook para la Tarea 4.
  - Overrides Python sin E/S:
    - `get_device_mode(self) -> str` devuelve `'0:0'`;
    - `set_device_mode(self, mode_id, param) -> None` no hace nada;
    - `restore_brightness(self) -> None` y `disable_brightness(self) -> None` no hacen nada.
- `class RazerBarracudaXChromaWired(_RazerHidrawDevice)`: `USB_PID = 0x0573`, el nombre de las
  Global Constraints y `METHODS = ['get_device_type_headset']`.
- `class RazerSeirenV3Mini(_RazerHidrawDevice)`: `USB_PID = 0x056A`, su nombre y
  `METHODS = ['get_device_type_accessory']`.
- `hidraw_fixtures.py`:
  - `make_sysfs_hid(root: str, sys_name: str, hidraw_name: Optional[str] = 'hidraw900', device_type: bool = False) -> str`:
    crea el directorio y devuelve su ruta.
  - `make_config() -> configparser.ConfigParser`: `[General] verbose_logging = False` y
    `[Startup]` con `sync_effects_enabled = True`, `devices_off_on_screensaver = True`,
    `restore_persistence = False`, `persistence_dual_boot_quirk = False`,
    `battery_notifier = False`.
  - `make_persistence() -> configparser.ConfigParser`, con `.status = {'changed': False}`.
  - `build_device(cls, dev_path: str)`: construye la clase dentro de
    `mock.patch.object(DBusService, '__init__', lambda self, object_path: None)` con los kwargs
    exactos que usa `daemon.py:495-499` (`device_number=0`, `testing=True`, listas vacías,
    `unknown_serial_counter={}`).

- [ ] **Step 1: Escribir las pruebas que fallan** en `test_hidraw_devices.py`:
  - `test_match_rules`:
    - `0003:1532:0574.0015` con `hidraw/hidraw900/` → True;
    - con `device_type` → False;
    - sin `hidraw/` → False;
    - `0005:1532:0574.0015` → False;
    - una ruta inexistente → False, sin excepción;
    - para `RazerSeirenV3Mini`, el id debe ir en mayúsculas (`0003:1532:056A.0003`).
  - `test_hidraw_node`: `'/dev/hidraw900'`.
  - `test_check_access`:
    - `testing=True` → True;
    - el Seiren (`OPENS_HIDRAW = False`) con `testing=False` → True sin llamar a `os.access`
      (comprobarlo con un mock);
    - la base por defecto de `RazerDevice` con un `device_type` creado por el usuario → True.
  - `test_passive_devices_publish_misc_surface`, para las dos clases:
    - `get_serial()` es `HIDRAW15320573` o `HIDRAW1532056A`;
    - `getDeviceName()` es el nombre;
    - `getFirmware()` es `'unknown'`;
    - `getDeviceType()` es `'headset'` o `'accessory'`;
    - `get_vid_pid()` es `[0x1532, PID]`;
    - `get_device_image()` es un `str`;
    - `getDeviceMode()` es `'0:0'`.
    - `addCleanup(dev.close)`.
  - `test_passive_devices_never_open_node`: `mock.patch('os.open')` no se llama durante la
    construcción.
  - `test_classes_registered`: `openrazer_daemon.hardware.get_device_classes()` contiene las dos
    clases (y la `Wireless` cuando exista la Tarea 4), y no contiene `_RazerHidrawDevice`.
- [ ] **Step 2: Comprobar que fallan.** `test_hidraw_devices`; esperado `ModuleNotFoundError`
  o `AttributeError`.
- [ ] **Step 3: Implementar** `check_access` y `HIDRAW_DEVICE` en `device_base.py`, los cambios
  de `daemon.py` y `hidraw_devices.py` como se describe en Interfaces.
- [ ] **Step 4: Comprobar.** `test_hidraw_devices` en OK y la suite completa en la línea base
  (24 + las nuevas, con 1 fallo que ya existía).
- [ ] **Step 5: Commit**: `daemon: Discover hidraw devices without a kernel driver`.

### Task 4: Barracuda X Chroma (Wireless): estado, eventos, D-Bus, reconexión y bloqueo

**Files:**
- Modify: `daemon/openrazer_daemon/hardware/hidraw_devices.py`: añadir
  `RazerBarracudaXChromaWireless`.
- Create: `daemon/tests/fake_hidraw.py`
- Test: `daemon/tests/test_barracuda_wireless.py`

**Interfaces:**
- Consume:
  - de la Tarea 1: los builders, `check_frame`, `parse_report` y `reply_matcher`;
  - de la Tarea 2: `HidrawTransport` y `open_hidraw`;
  - de la Tarea 3: `_RazerHidrawDevice`, `_extra_dbus_methods` y `hidraw_node`.
- `class RazerBarracudaXChromaWireless(_RazerHidrawDevice)`:
  - Atributos: `USB_PID = 0x0574`, `HIDRAW_DEVICE_NAME = 'Razer Barracuda X Chroma (Wireless)'`,
    `OPENS_HIDRAW = True` y
    `METHODS = ['get_device_type_headset', 'set_static_effect', 'set_none_effect', 'get_brightness', 'set_brightness', 'get_battery', 'is_charging', 'get_idle_time', 'set_idle_time']`.
  - `__init__(self, *args, **kwargs)`:
    1. **Antes** de `super().__init__`, crea `self._state_lock` y la caché: `_link`, `_battery`,
       `_charging`, `_idle_minutes`, `_pending_idle_minutes` y `_lights_suspended`. El
       BatteryNotifier puede llamar a `getBattery` dentro de ese constructor.
    2. Llama a `super().__init__`. La sincronización de efectos queda activa: `setStatic` y
       `setNone` sincronizados se aplican, y `setBrightness` sincronizado se ignora (ver
       Comportamiento).
    3. Abre el transporte con `self._open_fd()`. Si hay `OSError`, registra un warning y deja
       `self._transport = None`.
    4. Llama a `start()` y a `self._request_sync()`.
  - `_open_fd(self) -> int`:
    - con `self._testing`: lee el nombre de `<device_path>/fake_hidraw_socket`, conecta un
      socket `AF_UNIX`/`SOCK_SEQPACKET` abstracto (`'\0' + nombre`) y devuelve `sock.detach()`.
      Si el archivo no existe, lanza `FileNotFoundError`.
    - sin `_testing`: `open_hidraw(self.hidraw_node(self._device_path))`.
  - `_extra_dbus_methods(self)` devuelve estas siete tuplas, con aridad exacta:

    | Interfaz | Método | Implementación | In | Out |
    |---|---|---|---|---|
    | `razer.device.power` | `getBattery` | `_dbus_get_battery(self) -> float` | — | `d` |
    | `razer.device.power` | `isCharging` | `_dbus_is_charging(self) -> bool` | — | `b` |
    | `razer.device.power` | `getIdleTime` | `_dbus_get_idle_time(self) -> int` | — | `q` |
    | `razer.device.power` | `setIdleTime` | `_dbus_set_idle_time(self, idle_time)` | `q` | — |
    | `razer.device.lighting.chroma` | `setStatic` | `_dbus_set_static(self, red, green, blue)` | `yyy` | — |
    | `razer.device.lighting.chroma` | `setNone` | `_dbus_set_none(self)` | — | — |
    | `razer.device.lighting.brightness` | `setBrightness` | `_dbus_set_brightness(self, brightness)` | `d` | — |

    `getBrightness` y `getEffect*` se quedan como los da la base.
  - Comportamiento:
    - `getBattery`: el último valor conocido como `float`, o `-1.0`.
    - `isCharging`: `_charging and _link`.
    - `getIdleTime`: `_idle_minutes * 60`, o `0` si es desconocido.
    - `setIdleTime(s)`: convierte a minutos (Global Constraints). Con enlace,
      `write(build_set(0x2C, m))` y actualiza la caché; sin enlace, lo guarda en
      `_pending_idle_minutes`.
    - `setStatic(r, g, b)`: igual que `chroma_keyboard.set_static_effect` en `send_effect_event`,
      `set_persistence('backlight', 'effect', 'static')` y `zone['backlight']['colors'][0:3]`.
      Después, si hay enlace y no `_lights_suspended`, `write(build_color(r, g, b))`.
    - `setNone()`: `send_effect_event('setNone')` y `set_persistence(..., 'effect', 'none')`; si
      hay enlace, `write(build_color(0, 0, 0))`.
    - `setBrightness(b)`:
      - si `self.disable_notify` es True (llega de EffectSync), **retorna sin hacer nada**;
      - si no, ajusta b a 0..100, `set_persistence('backlight', 'brightness', b)`, y con enlace
        `write(build_set(0x41, int(round(b * 255 / 100))))`;
      - **no** llama a `send_effect_event`.
  - `_on_report(self, data) -> None` (hilo lector): `parse_report` y, si es un evento:
    - 0x21 → `_battery`;
    - 0x2A → `_charging = bool(value)`;
    - 0x20 → `_link = (value == 1)`. Si es 1, llama a `_request_sync()`; si es 0,
      `_charging = False`.
  - `_on_disconnect(self) -> None`: `_link = False`, `_charging = False`.
  - `_request_sync(self) -> None`: lanza `_sync` en un `threading.Thread(daemon=True)`. Si ya
    hay uno corriendo, marca que debe repetirse en lugar de lanzar otro.
  - `_sync(self) -> None`:
    1. `request(build_get(0x20))`. Si no hay respuesta o el valor no es 1, `_link = False` y
       termina.
    2. Si no, `_link = True` y GET de 0x21, 0x2A, 0x2C y 0x41 a la caché.
    3. Con 0x41, `zone['backlight']['brightness'] = round(v * 100 / 255, 1)`, sin escribir nada.
    4. Si hay `_pending_idle_minutes`, la escribe.
    5. Escribe `self._lighting_frame()` si no es `None`.
  - `_lighting_frame(self) -> Optional[bytes]`:
    - negro si `_lights_suspended` o si el efecto es `'none'`;
    - `build_color(*colors[0:3])` si el efecto es `'static'`;
    - `None` en cualquier otro caso. Así no se pisa la onda propia del headset cuando José
      todavía no eligió nada.
  - `_suspend_device(self)`: `_lights_suspended = True`; con enlace,
    `write(build_color(0, 0, 0))` directamente, **sin** tocar `zone`.
  - `_resume_device(self)`: `_lights_suspended = False`; con enlace, escribe `_lighting_frame()`.
  - `_close(self)`: `super()._close()` y luego `self._transport.close()` si existe.
- `fake_hidraw.py`:
  - `class FakeDongle`:
    - `__init__(self, props: Optional[dict] = None, silent: bool = False)`; por defecto
      `{0x20: 1, 0x21: 79, 0x2A: 0, 0x2C: 5, 0x41: 84}`;
    - `attach(self, sock: socket.socket) -> None`: hilo que sirve un extremo;
    - `listen_abstract(self, name: str) -> None`: acepta conexiones `SOCK_SEQPACKET` abstractas
      en un hilo;
    - `emit_event(self, prop: int, value: int) -> None`;
    - `writes` (lista de tramas recibidas);
    - `wait_for_writes(self, n: int, timeout: float = 2.0) -> bool`;
    - `close(self)`.

    Responde a cada GET con
    `02 02 60 00 00 00 05 00 80 80 P 01 01 V`, CRC XOR(1..61) en el byte 62, y a cada SET con
    el acuse `... (P|0x80) 01 01 00`, actualizando `props`. Toda trama recibida debe pasar
    `check_frame`; si no, la guarda en `rejected`. Con `silent=True` no responde nada.
  - `if __name__ == '__main__'`: `python3 fake_hidraw.py --abstract NAME [--log FILE]` sirve
    para siempre y escribe en `FILE` una línea hex por trama recibida (para la Tarea 5).
  - Helper `make_wireless(case, dongle) -> device`, también en `fake_hidraw.py`:
    - crea un directorio con `make_sysfs_hid(..., '0003:1532:0574.0001')` y `fake_hidraw_socket`
      con un nombre único;
    - llama a `dongle.listen_abstract(nombre)` y a `build_device(RazerBarracudaXChromaWireless, dir)`;
    - registra `case.addCleanup` para el dispositivo y el dongle.

- [ ] **Step 1: Escribir las pruebas que fallan** en `test_barracuda_wireless.py`.
  "Espera" significa sondear el estado con un timeout de 2 s.
  - `test_startup_sync_fills_cache`:
    - `getBattery()` llega a `79.0` y `isCharging()` es `False`;
    - `getIdleTime()` llega a `300`;
    - `getBrightness()` llega a `32.9`;
    - el dongle no recibió ningún SET 0xC1.
  - `test_battery_and_charging_events`:
    - `emit_event(0x21, 94)` → `94.0`;
    - `emit_event(0x2A, 1)` → `isCharging()` es True;
    - `emit_event(0x20, 0)` → `isCharging()` es False y `getBattery()` sigue en `94.0`.
  - `test_set_static_writes_color_and_persists`:
    - `setStatic(0, 0, 255)` → la última escritura es `build_color(0, 0, 255)`;
    - `getEffect()` es `'static'`;
    - `zone['backlight']['colors'][0:3] == [0, 0, 255]`.
  - `test_set_none_writes_black`.
  - `test_set_brightness_converts_and_persists`:
    - `setBrightness(50)` escribe `build_set(0x41, 128)`;
    - `setBrightness(150)` escribe `build_set(0x41, 255)`.
  - `test_idle_time_conversion`:
    - `setIdleTime(420)` escribe `build_set(0x2C, 7)` y `getIdleTime()` es `420`;
    - `setIdleTime(0)` escribe el valor 0;
    - `setIdleTime(7200)` escribe 60.
  - `test_link_up_reapplies_static`: tras `setStatic(0, 0, 255)`, `emit_event(0x20, 0)` y luego
    `emit_event(0x20, 1)`, la última escritura es `build_color(0, 0, 255)`.
  - `test_no_reapply_without_user_choice`: con persistencia vacía (efecto `'spectrum'`), el
    enlace no escribe ninguna trama de color.
  - `test_link_up_while_suspended_sends_black`: `suspend_device()`; enlace abajo y arriba →
    negro; `resume_device()` → `build_color(0, 0, 255)`; los colores de `zone` no cambian.
  - `test_synced_brightness_is_ignored`: con `dev.disable_notify = True`, `setBrightness(10)` no
    escribe nada y el brillo de `zone` no cambia.
  - `test_pending_idle_applied_on_link_up`: con el enlace abajo, `setIdleTime(600)` no escribe;
    al subir el enlace se escribe `build_set(0x2C, 10)`.
  - `test_construct_with_silent_dongle_is_fast_and_safe`:
    - con `FakeDongle(silent=True)` la construcción tarda < 0,5 s;
    - `getBattery()` es `-1.0` e `isCharging()` es `False`;
    - `setStatic(1, 2, 3)` no lanza.
  - `test_setters_after_disconnect_do_not_raise`: `dongle.close()`, espera a que `_link` sea
    False; `setStatic` y `setBrightness` no lanzan y `getEffect()` es `'static'`.
  - `test_open_failure_still_publishes_device`: sin archivo `fake_hidraw_socket`, construye
    bien, `getBattery()` es `-1.0` y `getDeviceName()` es el nombre.
  - `test_every_written_frame_passes_gate`: al final de cada prueba,
    `assertEqual(dongle.rejected, [])`. Ponlo en un `tearDown` común.
- [ ] **Step 2: Comprobar que fallan.** `test_barracuda_wireless`; esperado `ImportError` de
  `RazerBarracudaXChromaWireless`.
- [ ] **Step 3: Implementar** `fake_hidraw.py` y `RazerBarracudaXChromaWireless` como en
  Interfaces. Toda E/S de los setters va por `self._transport.write` dentro de un `if` que
  comprueba transporte y enlace; nunca usar `request` en el hilo de D-Bus.
- [ ] **Step 4: Comprobar.** Añade a `test_classes_registered` (Tarea 3) la aserción de que
  `RazerBarracudaXChromaWireless` está registrada. Ejecuta `test_barracuda_wireless`,
  `test_hidraw_devices` y la suite completa, que debe quedar en la línea base.
- [ ] **Step 5: Commit**: `daemon: Add Barracuda X Chroma wireless hidraw device`.

### Task 5: Prueba de extremo a extremo en un bus privado (cliente instalado + Polychromatic CLI)

**Files:**
- Create: `scripts/local/smoke-hidraw.sh`

**Interfaces:**
- Consume:
  - de la Tarea 4: `fake_hidraw.py --abstract NAME --log FILE`, y el modo testing de
    `_open_fd` (el archivo `fake_hidraw_socket`);
  - de la Tarea 3: el descubrimiento por `hidraw/` en `--test-dir`.

Uso: `scripts/local/smoke-hidraw.sh`. No tiene argumentos y sale con 0 solo si todo pasa.

Pasos del script:
1. **Bus privado.** Crea un directorio temporal `W` y levanta el bus privado así:

   ```bash
   systemd-socket-activate -l "@$NAME" -E DBUS_SESSION_BUS_ADDRESS="unix:abstract=$NAME" -E XDG_RUNTIME_DIR dbus-broker-launch --scope user --config-file "$W/bus.conf"
   ```

   - `bus.conf` es un busconfig de tipo `session`, auth `EXTERNAL` y una policy por defecto con
     `allow send_destination="*"`, `receive_sender="*"` y `own="*"`.
   - Exporta `DBUS_SESSION_BUS_ADDRESS=unix:abstract=$NAME`.
   - Hace `trap` de limpieza: matar el daemon, el dongle falso y el bus.
2. **sysfs falso.** Con `make_sysfs_hid` (vía `python3 -c` con `PYTHONPATH=daemon/tests`),
   crea en `W/td` los dispositivos `0003:1532:0574.0001` (con
   `fake_hidraw_socket` = `$NAME-dongle`), `0003:1532:0573.0002` y `0003:1532:056A.0003`.
3. **Dongle falso.** Arranca
   `python3 daemon/tests/fake_hidraw.py --abstract "$NAME-dongle" --log "$W/dongle.log"` en
   segundo plano.
4. **Daemon.** Arranca
   `PYTHONPATH=pylib:daemon python3 daemon/run_openrazer_daemon.py -F -v --run-dir "$W/run" --log-dir "$W/logs" --config daemon/resources/razer.conf --persistence "$W/persistence.conf" --test-dir "$W/td"`.
   - Antes hay que crear `persistence.conf` vacío.
   - Espera hasta 10 s a que
     `gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.NameHasOwner org.razer`
     dé `true`.
5. **Cliente instalado** (`env -u PYTHONPATH python3 -c ...`, el que usa Polychromatic): afirma
   que `DeviceManager().devices` tiene los 3 nombres exactos y que la `Wireless` tiene
   `has('battery')`, `has('lighting_static')`, `has('brightness')` y
   `battery_level == 79`.
6. **Polychromatic CLI** con `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` y `XDG_RUNTIME_DIR` dentro de
   `W` y `env -u PYTHONPATH`:
   - `polychromatic-cli -l --no-pretty-column` lista los 3 nombres;
   - `polychromatic-cli -n "Razer Barracuda X Chroma (Wireless)" -o static -c "#0000ff"` sale
     con 0;
   - `dongle.log` contiene la trama de `build_color(0, 0, 255)` en hex.
7. Imprime `SMOKE OK` y sale con 0.

- [ ] **Step 1: Escribir el script** según los pasos de arriba (`set -euo pipefail`; cada
  afirmación que falle imprime qué falló y sale con 1).
- [ ] **Step 2: Ejecutarlo:** `bash scripts/local/smoke-hidraw.sh`. Esperado: `SMOKE OK`.
  Comprueba también que el daemon real sigue dueño de `org.razer` en el bus de sesión real
  (`gdbus call --session ... NameHasOwner org.razer` → `true`, el mismo PID que antes).
- [ ] **Step 3: Si falla un caso de cliente o CLI, corregir en la tarea dueña** (3 o 4) con
  una prueba unitaria que reproduzca el fallo, y repetir el Step 2.
- [ ] **Step 4: Commit**: `scripts: Add private-bus smoke test for hidraw devices`.

### Task 6: Instalación local (regla udev y daemon del repo en lugar del empaquetado)

**Files:**
- Create: `install_files/udev/70-openrazer-hidraw-local.rules`, con exactamente:
  `KERNEL=="hidraw*", SUBSYSTEMS=="usb", ATTRS{idVendor}=="1532", ATTRS{idProduct}=="0574", TAG+="uaccess"`.
  Solo el dongle: los otros dos nunca se abren.
- Create: `scripts/local/install-local.sh`
- Create: `scripts/local/uninstall-local.sh`

`install-local.sh` hace, en orden y con eco de cada paso:
1. Comprueba que `bash scripts/local/smoke-hidraw.sh` da `SMOKE OK`; si no, aborta.
2. **Comprobación previa de los dispositivos que ya funcionan.** Los drivers instalados son
   3.12.4 y el daemon del repo es `master`.
   - Ejecuta `git fetch https://github.com/openrazer/openrazer.git tag v3.12.4 --no-tags`.
   - Para cada par (archivo, clase) — (`mouse.py`, `RazerBasiliskV3Pro35KWired`),
     (`mouse.py`, `RazerBasiliskV3Pro35KWireless`), (`keyboards.py`, `RazerBlackWidowV4Pro`) y
     (`accessory.py`, `RazerNommoChroma`) — muestra
     `diff <(git show v3.12.4:daemon/openrazer_daemon/hardware/$F | sed -n "/^class $C(/,/^class /p") <(sed -n "/^class $C(/,/^class /p" daemon/openrazer_daemon/hardware/$F)`.
   - Muestra `git diff --stat v3.12.4 HEAD -- daemon/openrazer_daemon/dbus_services/`.
   - Pide confirmación (`read -p`) antes de seguir.
3. `sudo install -m 644 install_files/udev/70-openrazer-hidraw-local.rules /etc/udev/rules.d/`,
   luego `sudo rm -f /etc/udev/rules.d/70-barracuda-probe.rules`, y después
   `sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw`.
4. Escribe `~/.config/systemd/user/openrazer-daemon.service.d/10-repo-daemon.conf`:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/bin/python3 <REPO>/daemon/run_openrazer_daemon.py -F
   Environment=PYTHONPATH=<REPO>/daemon
   Environment=PYTHONDONTWRITEBYTECODE=1
   ```

   `<REPO>` es la ruta absoluta del repo, resuelta con `git rev-parse --show-toplevel`.
5. `systemctl --user daemon-reload && systemctl --user restart openrazer-daemon`. Espera 5 s y
   muestra `systemctl --user status openrazer-daemon --no-pager` y las últimas 30 líneas de
   `~/.local/share/openrazer/logs/razer.log`.

`uninstall-local.sh` borra el drop-in, hace `daemon-reload` y `restart`, borra la regla udev y
hace `reload`/`trigger`.

- [ ] **Step 1: Escribir los tres archivos.**
- [ ] **Step 2: Revisión en seco:** `bash -n` de los dos scripts y `shellcheck` si existe (si
  no, saltarlo y anotarlo).
- [ ] **Step 3: Commit**: `scripts: Add local install of the repo daemon for hidraw devices`.
- [ ] **Step 4: Avisar a José y ejecutar con él** `bash scripts/local/install-local.sh`. El
  script pide sudo y confirmación. Tras el reinicio:
  - `polychromatic-cli -l --no-pretty-column` lista los dispositivos de siempre más los nuevos;
  - en `razer.log` no hay `Traceback`.

  Si algo falla, ejecutar `bash scripts/local/uninstall-local.sh` y volver a la tarea dueña.

### Task 7: Aceptación en hardware con José (spec §10)

**Files:** ninguno, salvo las correcciones que salgan; cada corrección va con su prueba en la
tarea dueña.

Hay que pedirle a José cada acción física y anotar el resultado de cada criterio:
1. En Polychromatic aparece `Razer Barracuda X Chroma (Wireless)` con la batería y el icono de
   carga. Compararlo con `gdbus call --session --dest org.razer --object-path /org/razer/device/HIDRAW15320574 --method razer.device.power.getBattery`.
2. Desde Polychromatic, probar en orden y que José confirme cada uno a la vista:
   - color fijo azul, luego rojo;
   - Off;
   - brillo al 100 y luego al 30;
   - "Sleep mode after" a 7 min, y `getIdleTime` por `gdbus` debe dar 420;
   - al final, devolver el tiempo a 5 min.
3. José apaga y enciende el headset: el último color vuelve en pocos segundos. Repetir tras
   `systemctl --user restart openrazer-daemon`.
4. Bloquear la pantalla: las luces se apagan; desbloquear: vuelve el color. `getBrightness` no
   cambia.
5. Conectar el cable: aparece `(Wired)` y la `Wireless` pasa a cargando (se ve al refrescar).
   Quitarlo: `(Wired)` desaparece.
6. `Razer Seiren V3 Mini` aparece como accesorio y no como "no reconocido".
7. El Basilisk (cable y receptor), el BlackWidow V4 Pro y el Nommo Chroma siguen funcionando:
   cambiar un efecto en cada uno y comprobar la batería del Basilisk.
8. Con el headset apagado, la lista de Polychromatic carga sin errores y no dice "cargando".
9. Reiniciar la máquina: todo funciona sin intervención y sin sudo.
10. En `razer.log` no hay errores repetidos ni GETs periódicos al headset. En una hora de uso,
    las escrituras al headset solo aparecen tras acciones de José o tras reconexiones.

- [ ] **Step 1: Recorrer los criterios 1–10 con José** y apuntar el resultado en el mensaje de
  cierre.
- [ ] **Step 2: Cada fallo:** prueba unitaria que lo reproduce en la tarea dueña, corrección,
  suite completa, `smoke-hidraw.sh`, y `systemctl --user restart openrazer-daemon`. Después,
  repetir el criterio.
- [ ] **Step 3: Limpieza.** Confirmar que `/etc/udev/rules.d/70-barracuda-probe.rules` ya no
  existe. El script de prueba del spike (en el scratchpad) no se versiona.
