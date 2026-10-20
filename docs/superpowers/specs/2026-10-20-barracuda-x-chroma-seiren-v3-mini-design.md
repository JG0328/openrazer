# Barracuda X Chroma y Seiren V3 Mini en Polychromatic (uso local)

Fecha: 2026-10-20 · Rama: `barracuda-seiren-local` · Estado: borrador para aprobación

## 1. Intención

José quiere controlar desde **Polychromatic** el RGB de su headset **Razer Barracuda X
Chroma** y ver su **batería y estado de carga**, igual que ya ve los de su mouse Basilisk
V3 Pro 35K. También quiere ajustar el **tiempo de apagado automático** del headset.
Además quiere que el **headset conectado por cable** y su micrófono **Razer Seiren V3 Mini**
aparezcan en Polychromatic como dispositivos propios, aunque no tengan nada que controlar.

Es **para su máquina**, no para enviar a OpenRazer oficial. Eso permite atajos, pero no
justifica nada frágil: tiene que sobrevivir a reinicios y no romper los dispositivos que ya
funcionan.

## 2. Dispositivos (verificado en esta máquina el 2026-10-20)

| USB ID | Qué es | Interfaces | Notas |
|---|---|---|---|
| `1532:0574` | **Dongle 2.4 GHz** del Barracuda X Chroma (USB-C). Por aquí va el audio y llegan los datos del headset | 0–2 audio USB (snd-usb-audio), **3 HID** | Manufacturer "MediaTek Inc", serial USB `0000000000000000`, alimentado por bus. Siempre enumerado, aunque el headset esté apagado o en Bluetooth |
| `1532:0573` | **El headset** conectado por su cable USB-C (carga y actualización de firmware; **no lleva audio**) | 0 HID ("AIROHA Speaker") | Autoalimentado, serial `0000000000000000`. Aparece solo con el cable puesto |
| `1532:056a` | **Seiren V3 Mini** (micrófono, sin RGB) | 0–1 audio USB, 2 HID | Ganancia y mute ya los maneja Linux (ALSA "Mic Capture Volume" −16..+15 dB, "Mic Capture Switch") |

Firmware del headset: **1.0.9.0** (el de lanzamiento). Su descriptor HID mide 191 bytes y
**no tiene HID LampArray**. Otras unidades con firmware más nuevo sí lo tienen (descriptor de
518 bytes), y con ellas el RGB funciona en OpenRGB; esa vía no aplica aquí.

Software instalado: paquetes Debian `openrazer-*` **3.12.4** y `polychromatic` **0.9.8**,
kernel 7.3, **Secure Boot activado**. El repo está en `master` @ `477f4d6d`, igual al `master`
oficial; las cadenas de versión del repo dicen 3.12.1.

## 3. Protocolo confirmado en esta unidad

Todo va por el **report HID 2** (página vendor `0xFF14`) del nodo hidraw del dongle `0574`.
Las tramas son de 64 bytes, con el report ID incluido. El dongle no tiene endpoint OUT de
interrupción, así que un `write()` a hidraw sale como control SET_REPORT (Output, wValue
`0x0202`). Las respuestas y los eventos llegan como input report 2. **usbhid solo sondea el
endpoint mientras alguien tiene abierto el nodo**, así que hay que mantenerlo abierto.

Trama de host:
```
[0]=02 [1]=00 [2]=60 [3..5]=00 [6]=SZ [7]=clase [8]=id [9]=80 (flag dongle) [10..61]=args [62]=XOR(bytes 0..61) [63]=00
```
- **Propiedad, GET:** clase/id 00/00, args `[P, 00, 00]`, SZ=04.
- **Propiedad, SET:** args `[P|0x80, 00, N, datos…]`, SZ=4+N.
- **Trama de color por LED:** clase/id **0F/03**, args `[00, 00, fila=0, inicio, fin, R,G,B, …]`,
  SZ=5+3n (la regla de OpenRGB). Funcionó con n=6, incluido el azul del último LED.

Respuesta: `02 02 60 00 00 00 05 00 80 80 <P> 01 01 <valor> …`, con byte 62 = XOR(bytes 1..61).
Evento espontáneo: `02 0A 60 <seq> 00 00 05 00 80 80 <P> 02 01 <valor> …`, con byte 62 =
XOR(bytes 1..61). Observado en esta unidad:
- **Al apagar el headset:** evento 0x20 = `00`, con seq 00.
- **Al encenderlo:** evento 0x20 = `01` y, en menos de 1 s, una foto del estado: 0x27 (no
  molestar), 0x55 (mute del mic) y 0x21 (batería). Ni 0x40 ni el color vienen en la foto.
- **Mientras carga:** 0x2A = `01` cada ~60 s, cada evento duplicado. La batería (0x21) llega
  al cambiar y en la foto de conexión.
- Por el mismo nodo llegan también los botones (report 0x0C, `0c 01` / `0c 00` para volumen,
  etc.), que no deben confundirse con respuestas.
- Con el headset en reposo, sin cambios, no llega nada; en 5 s no hubo ningún evento.

| Qué | Trama | Resultado en esta unidad |
|---|---|---|
| Enlace 2.4 GHz | GET 0x20 | `01` = enlazado |
| Batería | GET 0x21 | 79 %, 78 %, 77 % (0–100 directo) |
| Cargando | GET 0x2A | `00`; `01` con el cable puesto (se lee por el dongle) |
| Estado de luz | GET 0x40 | `0x0C` normal; pasa a `0x0A` tras una trama de color por LED |
| Brillo | GET 0x41 / SET 0xC1 `[v]` | Era 84; SET 255 se vio más intenso; **persiste tras apagar y encender** |
| Apagado automático | GET 0x2C / SET 0xAC `[min]` | Era 5 min; **acepta valores arbitrarios** (SET 7 → GET 7); 0 = nunca (según #2704) |
| Efecto onda (hardware) | SET 0xC0 `[04]` | Arcoíris en movimiento. Acuse OK; 0x40 siguió en 0x0C |
| Color por LED | 0F/03 con 6 LEDs | Rojo y patrón de 6 colores funcionaron, también desde el estado de onda (0x0C). **6 LEDs espejados**: cada índice se enciende en las dos orejeras. Un color fijo **se mantuvo 4 min** sin reenviarlo (0x40 = 0x0A todo el tiempo; José lo confirmó a la vista). **No persiste** tras apagar y encender |

Todos los SET recibieron acuse `02 02 … <P|0x80> 01 01 00` en unos 100 ms; la trama 0F/03 no
recibe acuse. Con 50 ms entre escrituras no hubo problemas. El headset **se apaga solo tras
5 min sin audio** (su ajuste actual), y entonces el dongle sigue enumerado pero deja de responder.

El headset por cable (`0573`) **no respondió a ningún GET** (0x20, 0x21, 0x2A, 0x40, 0x41), ni
con el flag 0x00 ni con 0x80.

Documentado para este modelo en #2704, pero no probado aquí: EQ (0x13/0x93, 0x15/0x95),
sidetone (0x18/0x98, 0x19/0x99), no molestar (0x27) y mute del mic (0x55, solo evento).

**Nunca tocar:**
- el report 6/7 de la página `0xFF13` (canal RACE de Airoha: flash/FOTA);
- ningún report Feature;
- el output report 5 (LED de telefonía);
- comandos SET no listados arriba.

## 4. Objetivos

1. **Barracuda vía dongle (`0574`)** aparece en Polychromatic como headset y ofrece:
   - batería en % e indicador de carga, como el Basilisk;
   - iluminación: **solo color fijo** (los 6 LEDs del mismo color), apagado y brillo;
   - tiempo de apagado automático con el control "Sleep mode after" que Polychromatic ya
     tiene (1–15 min), traducido a SET 0xAC en minutos;
   - al bloquear la pantalla, las luces del headset se apagan como las de los demás
     dispositivos (José tiene `devices_off_on_screensaver = True`) y vuelven al desbloquear.
2. **Barracuda por cable (`0573`)** aparece como dispositivo propio cuando el cable está
   conectado, sin controles. La batería y la carga se siguen viendo en el dispositivo del
   dongle.
3. **Seiren V3 Mini (`056a`)** aparece como dispositivo propio, sin controles, en lugar de
   "no reconocido".
4. Todo **arranca solo al iniciar sesión** y sobrevive a reinicios.

## 5. No objetivos

- Enviar un PR a OpenRazer oficial o pasar todo su CI. Seguir sus convenciones es deseable,
  pero no obligatorio.
- Modificar Polychromatic. Se usa el 0.9.8 instalado tal cual.
- **Efectos animados** (los de software de Polychromatic, que envían colores ~10 veces por
  segundo) y la **matriz por LED**. José los descartó para no arriesgar cortes de audio o
  caídas del enlace.
- El efecto onda de hardware (SET 0xC0 `[04]`). Funciona, pero José pidió solo color fijo.
- EQ, sidetone, no molestar y mute del mic en Polychromatic.
- Controles del Seiren (ganancia, mute, gestos de toque, LED).
- Batería por Bluetooth, porque por ahí no llegan datos.
- Firmware con LampArray u otras unidades. No hace falta soportarlos, pero el daemon no
  debe colgarse si el descriptor cambia.
- Un módulo de kernel nuevo.
- "Nunca" y más de 15 min de apagado en la interfaz: quedan fuera del deslizador.

## 6. Anti-objetivos (sería un fracaso aunque "funcione")

- Arriesgar el hardware: escribir en el canal RACE `0xFF13`, enviar reports Feature o SETs
  no verificados.
- Saturar el enlace RF con sondeos. En un BlackShark V3, preguntar cada ~2 s tumbaba el
  enlace. La batería debe venir de eventos y de lecturas esporádicas.
- Romper o ralentizar los dispositivos que ya funcionan (Basilisk V3 Pro 35K con su receptor,
  BlackWidow V4 Pro, Nommo Chroma).
- Que Polychromatic muestre datos falsos:
  - "cargando" cuando no se sabe (el daemon hace `bool(int(charge_status))`, así que −1 se ve
    como cargando);
  - porcentajes mal redondeados.
- Que un error de D-Bus del headset (por ejemplo, apagado) haga fallar la lista entera de
  dispositivos en Polychromatic, que no protege esas llamadas.
- Necesitar sudo o un daemon como root en el uso diario.
- Que una actualización del paquete Debian deshaga la instalación en silencio o deje dos
  daemons peleando por `org.razer`.

## 7. Restricciones conocidas

- **Descubrimiento del daemon.** Hoy solo reconoce dispositivos con un driver de kernel que
  crea el archivo `device_type` en sysfs (`daemon/openrazer_daemon/hardware/device_base.py:1294-1313`),
  y su monitor udev filtra `subsystem='hid'` (`daemon/openrazer_daemon/daemon.py:205-209`).
  Estos tres dispositivos no tienen driver OpenRazer; están en hid-generic.
- **Acceso.** La regla udev `uaccess` ya instalada en `/etc/udev/rules.d/70-barracuda-probe.rules`
  da acceso al usuario a los hidraw de `0573`, `0574` y `056a`. Hoy la usa la prueba; la
  solución final la deja permanente, con el nombre que convenga.
- **Seriales repetidos.** `0573` y `0574` reportan `0000000000000000` y el Seiren no tiene
  serial. El daemon publica cada dispositivo en D-Bus con su serial, y Polychromatic guarda
  sus preferencias por serial, así que **cada dispositivo necesita un serial único y estable**
  entre reinicios.
- **Polychromatic 0.9.8:**
  - Muestra la batería si el dispositivo tiene la capacidad `battery`, que la librería cliente
    deduce de `getBattery`. Lee `isCharging` y `getBattery` cuando arma la lista, no en vivo.
  - `getBattery` = −1 se ve como "−1%".
  - El deslizador de apagado va de 1 a 15 min y envía segundos (`set_idle_time(min*60)`;
    `get_idle_time()` lo divide entre 60).
  - Los iconos salen del tipo de dispositivo. No existe "microphone"; los disponibles son
    `accessory`, `headset`, `speaker`, etc.
  - `DEVICE_IMAGE` no puede ser `None`.
- **Librería cliente.** Polychromatic importa `openrazer.client` del paquete del sistema
  (`python3-openrazer`). Si la solución necesita cambiarla, hay que resolver cómo la carga
  Polychromatic. Lo preferible es no cambiarla.
- **Daemon del sistema.** Es `/usr/bin/openrazer-daemon -F`, lanzado por la unidad de usuario
  `openrazer-daemon.service` (`Type=dbus`, `BusName=org.razer`). Hoy no está cargado ningún
  módulo `razerkraken`.
- **El headset se apaga solo.** Pasados los minutos configurados, el enlace cae y vuelve al
  encenderlo, con los eventos 0x20 de la sección 3. El color hay que reaplicarlo, pero el
  brillo y el apagado automático quedan guardados en el headset.
- **Configuración del daemon de José** (`~/.config/openrazer/razer.conf`):
  - `restore_persistence = False`
  - `devices_off_on_screensaver = True`: el daemon lo implementa poniendo brillo 0, lo que en
    el headset quedaría guardado.
  - `battery_notifier = True`, con `battery_notifier_freq = 600` y
    `battery_notifier_percent = 33`. El BatteryManager pregunta `getBattery` cada 600 s, y
    cada ~10 s mientras el valor es −1 o 0.

## 8. Decisiones tomadas

**De José:**
- Uso local, no upstream.
- El Seiren y el headset por cable aparecen sin controles.
- Polychromatic no se modifica.
- El tiempo de apagado se controla con el deslizador existente.
- Iluminación: solo color fijo, sin efectos animados ni onda.
- Al bloquear la pantalla, el headset apaga sus luces.
- **Durante el desarrollo** se puede usar su hardware real con los comandos de la sección 3 y
  parar a ratos el daemon instalado. José está disponible para pasos físicos en puntos
  intermedios: apagar y prender el headset, el cable, Bluetooth, reiniciar.

**Mías (revisables):**
- **Todo en el daemon, en espacio de usuario**, hablando por hidraw, sin módulo de kernel.
  Evita Secure Boot y DKMS, evita chocar con el paquete `openrazer-driver-dkms`, y si algo
  falla se reinicia el daemon en vez de colgarse el kernel.
- **Se usa el daemon de este repo** en lugar del instalado, de una forma que sobreviva a
  actualizaciones del paquete (por ejemplo, un override de la unidad systemd de usuario).
  Los dispositivos existentes siguen usando los drivers de kernel del paquete Debian, como hoy.
- **Reconexión basada en eventos, sin sondeo.** El daemon mantiene abierto el nodo del dongle
  y escucha. Cuando llega el evento 0x20 = `01`, cuando el dongle reaparece o cuando arranca
  el daemon con el headset enlazado, **reaplica el último color** (y el apagado de luces si la
  pantalla está bloqueada). Esto vale **aunque `restore_persistence = False`**: esa opción
  existe para no pisar lo que los dispositivos guardan ellos mismos, y este headset no guarda
  el color.
- **Batería y carga** salen de los eventos 0x21/0x2A, guardados en caché. Además se hace un
  GET 0x21 y uno 0x2A al enlazar o al arrancar, para no depender de que llegue la foto. Las
  llamadas `getBattery`/`isCharging` del daemon y de Polychromatic leen la caché; **nunca**
  generan tráfico RF.
- **Batería desconocida:** si el headset está apagado o en Bluetooth, se informa el último
  valor conocido y "no cargando". Si nunca se conoció, −1.
- **"Apagado" de luces = trama de color negra**, no brillo 0, tanto para el apagado elegido
  por José como para el bloqueo de pantalla. Así no se guarda un brillo 0 en el headset y se
  conserva el brillo elegido.
- **Nombres:**
  - "Razer Barracuda X Chroma (Wireless)" para `0574`;
  - "Razer Barracuda X Chroma (Wired)" para `0573`;
  - "Razer Seiren V3 Mini" para `056a`, con tipo `accessory`.
- **El brillo** (SET 0xC1) se guarda en el headset. El daemon lo aplica cuando José lo cambia,
  pero no lo reenvía en cada reconexión, porque ya está guardado.

## 9. Lo que queda al constructor

- Cómo se integra el descubrimiento de dispositivos sin driver de kernel en el daemon, y cómo
  se detectan altas y bajas (hotplug).
- Dónde vive el transporte hidraw: hilo, `GLib.io_add_watch` u otra cosa. También cómo se
  emparejan respuestas y eventos, y cómo se mantiene el nodo abierto.
- Cómo se generan seriales únicos y estables.
- Cómo se guarda y reaplica la iluminación: reutilizar la persistencia que el daemon ya tiene
  es lo esperable, respetando las decisiones de la sección 8.
- Cómo se engancha el headset al bloqueo de pantalla sin que el camino genérico ponga brillo 0.
- Cómo se instala y se lanza el daemon del repo en esta máquina, y cómo se vuelve atrás.
- Pruebas: el repo ya tiene un sistema de dispositivos falsos (`pylib/openrazer/_fake_driver`,
  `scripts/ci/`) y un dispositivo hidraw falso sería razonable.
- Imagen del dispositivo.

## 10. Cómo sabremos que funciona

1. En Polychromatic aparece **Razer Barracuda X Chroma (Wireless)** con su batería en % y el
   icono de carga correcto, que coincide con GET 0x21/0x2A.
2. Desde Polychromatic se puede:
   - poner un color fijo y apagar las luces;
   - cambiar el brillo;
   - mover "Sleep mode after", y GET 0x2C devuelve los minutos elegidos.
3. Se apaga y se enciende el headset: el último color vuelve solo en pocos segundos.
   Reiniciar la máquina o el daemon también lo devuelve.
4. Al bloquear la pantalla se apagan las luces del headset; al desbloquear vuelve el color, y
   el brillo guardado en el headset no cambia.
5. Se conecta el cable: aparece **(Wired)**, sin controles. Se quita: desaparece. El
   dispositivo del dongle sigue mostrando la batería y pasa a "cargando".
6. El **Seiren V3 Mini** aparece como dispositivo propio y no como "no reconocido".
7. El Basilisk (cable y receptor), el BlackWidow V4 Pro y el Nommo Chroma siguen funcionando
   igual que antes.
8. Con el headset apagado, Polychromatic sigue listando todo sin errores y no dice "cargando".
9. Tras reiniciar la máquina todo sigue funcionando sin intervención y sin sudo.
10. En el log del daemon no hay errores repetidos ni sondeos frecuentes al headset.

## Referencias

- openrazer/openrazer#2704: protocolo del Barracuda X Chroma (eventos y captura de Synapse).
- openrazer/openrazer#2882 (Kraken Kitty V3 Pro) y #2794 (BlackShark V3): implementaciones
  del mismo sobre de 64 bytes en plataformas MediaTek hermanas.
- OpenRGB `RazerKrakenV4Controller`: comandos de brillo, modo y color por LED.
- BatteryWatch (itayavra/batterywatch): lectura de batería del 0574 en Linux.
