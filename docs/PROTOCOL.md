> Protocolo de los ventiladores SpinDisplay F56, reconstruido para interoperabilidad y verificado en hardware.
> No incluye código del fabricante.

# FINDINGS — Protocolo SpinDisplay / F56

> Actualizado: 2026-10-21. Fuente principal: `SpinDisplay.exe` decompilado con ilspycmd 8.2 en `(código oficial decompilado, no incluido)/`.
> Leyenda: **[CÓDIGO]** leído en el decompilado · **[OBSERVADO]** visto en la PC/red real · **[HIPÓTESIS]** sin confirmar.

## 1. Software

- **[CÓDIGO]** `SpinDisplay.exe` es .NET Framework 4.6.1 (WPF), namespace `F20AD`, sin ofuscar. Usa turbojpeg, FFmpeg.AutoGen y SQLite.
- **[CÓDIGO]** La instancia única (`F20AD/App.cs`, `OnStartup`) solo busca otro proceso con el mismo nombre, le da foco y se cierra. No hay mutex ni puerto reservado para eso.
- **[CÓDIGO]** Al arrancar, la app agrega reglas al firewall de Windows (`FireWallHelp.NetFwCheck`).

## 2. Puertos

| Puerto | Proto | Dirección | Uso |
|---|---|---|---|
| 50100 | UDP | PC → broadcast `a.b.c.255` | descubrimiento y verificación de contraseña |
| 50105 | UDP | equipo → PC (la PC escucha) | respuestas de descubrimiento |
| 50110 | TCP | PC → equipo | canal de **control** (comandos, heartbeat) |
| 50115 | TCP | PC → equipo | canal de **datos** (archivos, firmware, **proyección**) |

- **[OBSERVADO]** Con SpinDisplay abierto y 2 equipos conectados: hay conexiones TCP establecidas `PC:efímero → 192.168.x.144:50110` y `→ 192.168.x.106:50110`, y la app escucha en `0.0.0.0:50105/udp`.
- **[OBSERVADO]** Curiosidad: SpinDisplay deja abiertos decenas de sockets UDP efímeros (un socket de broadcast por segundo que nunca cierra). Es una fuga menor de la app oficial, sin impacto en el diseño.

## 3. Formato de trama de comando

```
[0x68][cmd][len lo][len hi][payload … len bytes][crc lo][crc hi]
```

- CRC-16/MODBUS (init 0xFFFF, poly reflejado 0xA001) calculado sobre todo lo anterior al CRC. **[CÓDIGO]** `F20AD.Utils/Crc16.cs`. Verificado: la implementación Python coincide byte a byte con las tablas del C# en 2000 casos aleatorios.
- Las respuestas del equipo usan el mismo formato (`TcpServer.AnalyticCommand`, longitud total = len + 6).

Comandos relevantes (`F20AD.Connect/Protocol.cs`, `Send.cs`):

| cmd | Nombre | Payload | Notas |
|---|---|---|---|
| 0x00 | UdpBroadcast | 4 bytes aleatorios LE | por UDP a :50100 |
| 0x01 | UdpConnect (respuesta) | ≥45 bytes | estado, ¿contraseña?, nombre[24], MAC[6] en 31, type 37, ver 38, IP de la PC vista por el equipo en 39–42 |
| 0x02 | UdpPasswordCheck | 8 bytes: contraseña XOR aleatorio | solo si el equipo tiene contraseña |
| 0x06 | OtherControl | 1 byte (0/1) | casilla **Third party control**: la PC solo activa una bandera; la API externa vive en el firmware y no aparece en esta app |
| 0x10 | Connect | `01` | handshake TCP; se repite cada 200 ms hasta recibir un 0x10 |
| 0x11 | QueryVersion | — | |
| 0x12 | QueryParameter | — | respuesta ≥330 bytes con nombre, modelo, `max_fps`, `content_type`, `jpg_size`, banderas. **Incluye SSID y contraseñas Wi-Fi en texto plano**: no registrar esa respuesta en logs compartidos |
| 0x14 | Heart | — | cada 2 s; si en ~20 s no hay respuesta, la app cierra la conexión |
| 0x37 | **Projection** | `01` iniciar / `00` detener | respuesta `68 37 .. .. XX`: XX=0 significa aceptado |
| 0x3B | Transcoding | — | parámetros polares; solo se piden si `content_type == 3` |

Ejemplos de trama: `Connect = 68 10 01 00 01 d1 09`, `Heart = 68 14 00 00 5c 40`, `Projection(1) = 68 37 01 00 01 db bd`.

## 4. Flujo de Projection (**[CÓDIGO]** `ProjectionWindow.cs`, `TcpServer.cs`)

1. Descubrimiento UDP → por cada equipo, `TcpServer.Connect`: TCP 50110, handshake 0x10, QueryVersion, QueryParameter, heartbeat.
2. `StartProjection`: abre **una conexión TCP nueva a :50115** y manda `Projection(1)` por el canal de control cada 2 s hasta que el equipo responda 0x37 con estado 0.
3. Hilo de captura: `Graphics.CopyFromScreen` de la **pantalla principal**, escala a **672×672** sin conservar proporción. Cola de máximo 2 cuadros (descarta los viejos).
4. Hilo de envío, cada ~10 ms: el cuadro se comprime y se manda por :50115 como
   ```
   [0xFA][longitud 3 bytes LE][datos]
   ```
   - Si `content_type != 3`: datos = **JPEG normal 672×672** (turbojpeg, BGR).
   - Si `content_type == 3`: datos = JPEG de una matriz **polar ya muestreada** (`JpgBinTool`: LEDs × rayos, con parámetros del comando Transcoding). Es decir, en ese modo la PC hace la conversión a la geometría del ventilador.
5. `CancelProjection`: `Projection(0)` y cierre de :50115.

**Consecuencias de diseño**

- **[CÓDIGO]** Cada equipo tiene su propio objeto `TcpServer` con sockets independientes. La limitación de "un solo ventilador" está en la **ventana** (`ProjectionWindow` tiene un solo campo `device`), no en el protocolo de la PC.
- **[CÓDIGO]** No hay cifrado, codec de video ni marcas de tiempo: es MJPEG sobre TCP con una cabecera mínima. No existe sincronización entre equipos; cada stream es independiente.
- **[CÓDIGO]** El FPS lo marca la PC; el equipo informa `max_fps` (20 por defecto si reporta 0).
- **[HIPÓTESIS]** El firmware podría aceptar un solo cliente de control a la vez (la respuesta UDP indica "conectado a PC/Android/iPhone"). Para probar nuestro cliente, **cerrar SpinDisplay** primero.
- **[HIPÓTESIS]** Pertenecer a un Sync Group no debería impedir la proyección (el código no lo comprueba), pero falta probarlo.

## 5. Pruebas en hardware (2026-10-22, PC por Ethernet 192.168.x.27, F56 "1-1" .144 y "2-1" .106)

**[OBSERVADO] Comportamiento del firmware que el código no muestra:**

- El F56 **cierra el TCP 50110 de inmediato si antes no recibió un broadcast UDP de descubrimiento**. El cliente manda uno justo antes de conectar (`announce()`).
- Si recibe un `Connect` (0x10) repetido, corta la conexión (SpinDisplay lo repite cada 200 ms, pero quizá le gana la respuesta). El cliente lo manda una sola vez.
- Después de cerrar una sesión, el siguiente intento suele fallar y el posterior funciona (alterna). El cliente reintenta hasta 4 veces.
- La primera respuesta a `Projection(1)` puede ser el código 1 (rechazo), y al reintentar se acepta.

**[OBSERVADO] Parámetros reportados por ambos equipos:** modelo `F56`, `content_type = 0` (**JPEG cuadrado 672×672, sin transcodificación polar**), `max_fps = 20`, `diam = 558`, `projection_disable = 0`. Respuesta de versión: `68 11 04 00 02 21 02 02 …`.

**[OBSERVADO] Proyección con `spinclient.py` (patrón de prueba, JPEG calidad 30 como la app original, unos 21 KB por cuadro):**

| Prueba | Resultado |
|---|---|
| 1 equipo (2-1), 30 s | **20 fps sostenidos, 3.5 Mbit/s, 0 cuadros descartados** |
| 2 equipos a la vez, 60 s | 2-1: 60 s completos a ~19 fps (pocos descartes al inicio). 1-1: 13–17 fps durante ~25 s; luego se trabó y dio timeout de envío |
| 1-1 solo, calidad 85 (43 KB) | ~4 fps y luego timeout |

**[OBSERVADO] Causa probable del problema de 1-1: su enlace Wi-Fi.** Ping de 1400 bytes: 1-1 pierde **20–36%**, 2-1 pierde **0%** (pings pequeños, 0% en ambos). La pérdida de paquetes grandes desploma el rendimiento de TCP.

**Conclusiones:**

- **Éxito mínimo alcanzado en lo técnico:** dos F56 recibieron al mismo tiempo video en vivo distinto desde una sola PC. La estabilidad de 1-1 depende de mejorar su enlace de radio.
- Ancho de banda por equipo a 20 fps y calidad 30: **~3.5 Mbit/s**. Para 5 equipos: ~18 Mbit/s de aire en el AP, viable en 2.4 GHz si la señal es buena.
- Falta confirmación visual del usuario (que el patrón se vea bien en el ventilador) y medir la latencia.

## 5b. Control del equipo (2026-10-25)

- **[OBSERVADO]** `0x17` OnOff con payload `01` / `00` **enciende y apaga el motor** (lo mismo que el botón de encendido del control remoto). Confirmado por el usuario desde SpinBridge.
- **[CÓDIGO]** `0x13` Brillo: payload 0..5 = 50 %..100 % (`SettingWindow`: `50 + n*10`). El valor actual está en el byte 294 de la respuesta de parámetros.
- **[OBSERVADO]** Se puede detener y reiniciar `Projection` varias veces dentro de la misma sesión de control, sin reconectar.

## 5c. Capacidad con 5 ventiladores (2026-10-25)

- **[OBSERVADO]** Con 5 F56 proyectando a la vez, el total entregado se queda en **~5.5-7 Mbit/s** sin importar el router: AX11000 ~5.5, GT6 ~6.8, UniFi ~5.5. Enlaces de 72 Mbps, 0 % de pérdida y retransmisiones TCP < 0.2 %, así que el límite está del lado de los ventiladores, no de la señal.
- **[OBSERVADO]** El límite es de **bytes**, no de cuadros: con cuadros más livianos suben los fps en la misma proporción.
- **[OBSERVADO]** Un JPEG 672×672 nunca pesa menos de ~7.5 KB, aunque sea todo negro (el mínimo lo pone la cantidad de bloques). Contenido holo con máscara circular: ~11-14 KB por cuadro.
- **[OBSERVADO]** **JPEG con Huffman optimizado**: ~30 % más chico, pero **el F56 rechaza algunos cuadros así y muestra barras SMPTE**. Queda desactivado.
- Con un presupuesto total de 6 Mbit/s y redistribución de lo que no usan los ventiladores con contenido oscuro, se logran ~16 fps de promedio con los 5 (contenido holo, JPEG optimizado). Falta medirlo sin optimizar.

- **[OBSERVADO] Ruijie RAP73 Pro** (AP enterprise, cable a la UniFi, canal 6, 20 MHz): capacidad real **~8.2-8.7 Mbit/s** con 5 ventiladores (+50 % sobre los routers de consumo). Contenido holo: **15.7 fps** con presupuesto de 6 Mbit/s y **17.4 fps** con presupuesto de 8 Mbit/s. Es el primer equipo que rompe el techo de ~6 Mbit/s, así que el límite depende en buena parte de cómo el AP maneja el aire, no solo de los ventiladores.

## 6. Pendiente

- [ ] Confirmación visual del patrón en los ventiladores y medición de latencia.
- [ ] Diagnosticar 1-1: intercambiar posiciones de 1-1 y 2-1 para saber si es la ubicación o la unidad; revisar canal/RSSI en el router.
- [ ] Prueba larga (30–60 min) con los 2 equipos.
- [ ] Comportamiento con contraseña UDP activada (0x02 no implementado).
- [ ] Captura de Wireshark de SpinDisplay proyectando (opcional; el protocolo ya funciona).

