# Video en vivo a 5 ventiladores holográficos Wi-Fi: estado y consulta técnica

> Fecha: 25 de octubre de 2026
> Propósito: pedir opinión experta sobre el **cuello de botella de red con 5 ventiladores simultáneos**.
> Convención: **[MEDIDO]** observado en hardware real · **[CÓDIGO]** leído del software oficial decompilado · **[HIPÓTESIS]** sin confirmar.

---

## 1. Resumen ejecutivo

- Objetivo: un show en vivo donde **Resolume Arena** controla una pantalla LED de fondo (1920×1152) y **5 ventiladores holográficos** (SpinDisplay F56) frente a ella, cada uno con su propio contenido en tiempo real.
- Logramos reconstruir el protocolo de proyección del fabricante y construimos una app propia (**SpinBridge**) que toma la salida de Resolume por Spout y la envía a los 5 ventiladores por Wi-Fi.
- **Funciona**: los 5 reciben video en vivo distinto, sincronizado desde una sola PC.
- **Problema**: con los 5 transmitiendo a la vez, el total entregado se estanca en **~5.5-7 Mbit/s, sin importar el router** (probamos 3). Cada ventilador por separado acepta ~3.8 Mbit/s. Con contenido real logramos **~13 fps por ventilador** (objetivo: 20).
- **Pregunta principal para el experto:** ¿qué limita el agregado a ~6 Mbit/s cuando cada enlace está a 72 Mbps PHY sin pérdidas, y cómo subirlo?

---

## 2. Hardware

| Elemento | Detalle |
|---|---|
| Ventiladores | 5 × SpinDisplay **F56** (56 cm, 24 V / 72 W), firmware 2.3.3 |
| Wi-Fi de los F56 | Solo 2.4 GHz [HIPÓTESIS: chip simple tipo ESP32/Realtek, 1×1]. Enlace negociado: **Tx 72.2 / Rx 43.3 Mbps** (802.11n MCS7, 20 MHz, GI corto) |
| PC de pruebas | Laptop Windows 11, RTX 4070 + Intel UHD, **Ethernet 1 Gbps** al router |
| PC de producción | Media Server Windows 11, RTX 3060 (Resolume + procesador Novastar + monitor), Radeon VisionTek de 6 salidas |
| Routers probados | ASUS ROG Rapture **GT6** (un solo nodo), ASUS ROG **GT-AX11000**, **Ubiquiti UniFi** (cubo, probablemente UniFi Express) |
| Router pendiente | TP-Link **BE19000** (Wi-Fi 7) |

---

## 3. Protocolo del ventilador (reconstruido)

Fuente: `SpinDisplay.exe` (.NET 4.6.1, sin ofuscar) decompilado con ILSpy y verificado en hardware.

| Canal | Puerto | Uso |
|---|---|---|
| UDP broadcast | 50100 (PC → equipos), respuestas a 50105 | Descubrimiento |
| TCP | **50110** | Control: tramas `[0x68][cmd][len16 LE][payload][CRC16-MODBUS]`, heartbeat cada 2 s |
| TCP | **50115** | Datos: cada cuadro va como `[0xFA][longitud 24 bits LE][JPEG]` |

**Proyección [CÓDIGO]:** la app oficial captura la pantalla con GDI, la escala a **672×672** y la comprime a **JPEG calidad 30 (4:2:0)** con turbojpeg. La envía por TCP sin cifrado, sin codec de video y sin marcas de tiempo (es MJPEG sobre TCP). Cada equipo tiene su propia conexión; la app oficial solo permite proyectar a uno a la vez, pero es una limitación de la interfaz, no del protocolo.

**Comportamiento del firmware [MEDIDO], no documentado:**
- Cierra el TCP 50110 si no recibió un broadcast UDP de descubrimiento poco antes.
- Corta la sesión si recibe el comando Connect repetido.
- Tras cerrar una sesión, el siguiente intento de conexión suele fallar y el posterior funciona.
- Reporta `max_fps = 20` y `content_type = 0` (acepta JPEG cuadrado; no requiere la conversión polar que el software hace para otros modelos).
- `0x17` OnOff = **motor** encendido/apagado (confirmado). `0x13` = brillo 0..5 (50-100 %).
- La proyección se puede detener y reiniciar dentro de la misma sesión de control.
- **Rechaza algunos JPEG con Huffman optimizado** (`IMWRITE_JPEG_OPTIMIZE`): muestra **barras SMPTE**. Con tablas Huffman estándar no pasa.

---

## 4. Lo que implementamos

### 4.1 Cadena completa
```
Resolume Arena (composición 1920×1152)
  ├─ Screen 2 → LED (Novastar, 1920×1080)
  └─ Screen 1 → Spout 3360×672: 5 slices de 672×672, cada uno alimentado por una capa
        │ (textura compartida en GPU, misma PC)
        ▼
SpinBridge (Electron + engine Python)
  recibe Spout → recorta 5 slices → máscara circular → JPEG → TCP a cada F56 (un hilo por equipo)
```

### 4.2 SpinBridge
- **Instalador para Windows** (Electron + engine empaquetado con PyInstaller). No requiere Python en el server.
- **Interfaz:** RUN/STOP global y por ventilador, autodetección, asignación de ventilador a slot arrastrando (se guarda por MAC, sobrevive a cambios de IP o de router), botón "Identificar" (muestra el número en el ventilador), motor ON/OFF con confirmación, brillo, vista previa de Spout y de lo que recibe cada ventilador, y métricas por equipo (fps, Mbit/s, KB por cuadro, calidad, latencia, descartes).
- **API HTTP** (`/api/run`, `/api/stop`, `/api/power`, `/api/identify`) para Stream Deck o Companion.
- **Robustez:** reconexión automática; si la app muere, el engine detiene la proyección y sale; detecta Spout en negro o en otra GPU.
- **Envío:**
  - **Descarte de cuadros**: si el anterior no salió, se reemplaza; no se acumula retraso.
  - **Calidad JPEG automática** por ventilador para ajustarse a un presupuesto de bytes.
  - **Presupuesto total compartido** (por defecto 6 Mbit/s): cada ventilador tiene garantizado total/N y además usa lo que los demás no ocupan.
  - **Máscara circular**: las esquinas no se ven en un disco giratorio; ahorra ~8 %.

---

## 5. Mediciones

### 5.1 Un ventilador solo
| Router | Resultado |
|---|---|
| GT6 | 20 fps con cuadros de ~21 KB ≈ **3.5 Mbit/s**; se observaron hasta ~4-5 Mbit/s |
| UniFi | 15 fps con cuadros de ~31 KB ≈ **3.8 Mbit/s** |

Calidad JPEG 85 (43 KB por cuadro) → ~4 fps. El techo por equipo ronda **3.5-4 Mbit/s** [HIPÓTESIS: ventana TCP chica del ventilador y/o velocidad de su decodificador].

### 5.2 Escalado con varios ventiladores (UniFi, carta de ajuste de Resolume, ~31 KB por cuadro)
| Ventiladores transmitiendo | Por ventilador | **Total** |
|---|---|---|
| 1 | 3.8 Mbit/s | 3.8 |
| 2 | ~2.2 | 4.4 |
| 3 | ~1.2 | 3.5 |
| 5 | ~0.76 | 3.8 |

(Medido primero en el AX11000 en canal 1, compartido con otro router de la casa. Después: canal 6 y la red de la casa apagada.)

### 5.3 Comparación de routers, 5 ventiladores, mismo contenido (carta de ajuste)
| Router | fps por ventilador | **Total entregado** | Latencia con carga (sin carga: 3-10 ms) | Retransmisiones TCP |
|---|---|---|---|---|
| GT-AX11000 (canal 6, 20 MHz, sin otra red en 2.4) | 4.3 | **~5.5 Mbit/s** | ~50 ms | 0.16 % |
| GT6 (un nodo) | 5.4 | **~6.8 Mbit/s** | 35-55 ms | 0 % |
| UniFi | 4.3 | **~5.5 Mbit/s** | ~40 ms | 0.06 % |

- Con cuadros más chicos (17 KB) los fps subieron de 4.3 a 7.9 y el total siguió en ~5.5 Mbit/s: **el límite es de bytes, no de cuadros**.
- La PC no limita: el engine recibe ~50 cuadros/s de Spout, CPU 10-28 %, Ethernet a 1 Gbps.
- `ping` de 1400 B a los ventiladores durante la carga: 0 % de pérdida, 20-110 ms.

### 5.4 Tamaño de los cuadros (672×672, máscara circular)
| Contenido | JPEG estándar | Huffman optimizado |
|---|---|---|
| Negro total | **7.5 KB** (mínimo que imponen ~7 000 bloques) | 2.9 KB |
| Videos holo, calidad 60 | 14.4 KB (p95 24.9) | 10.8 KB |
| Videos holo, calidad 30 | 11.9 KB (p95 19.9) | 8.0 KB |
| Carta de ajuste, calidad 20 | 25.8 KB | 20.9 KB |

### 5.5 Resultado con contenido real (videos holo, UniFi, 5 ventiladores, presupuesto total de 6 Mbit/s)
| Configuración | fps promedio por ventilador | Observaciones |
|---|---|---|
| JPEG estándar | **12.8** (10.8-14.2) | Estable, sin artefactos |
| Huffman optimizado + redistribución | ~16-17 | **Aparecen barras SMPTE** de vez en cuando → descartado |

---

## 6. Hipótesis descartadas

| Hipótesis | Cómo se descartó |
|---|---|
| PC / SpinBridge / CPU | Sobran cuadros de origen, CPU baja, cable a 1 Gbps |
| Señal débil de los ventiladores | Enlace a 72.2 Mbps, 0 % de pérdida de ping |
| Interferencia con la red de la casa | Canal 1 → 6 y 2.4 GHz de la casa apagado: mejora mínima (~4 → 5.5) |
| Airtime Fairness (ASUS) | Ya estaba apagado |
| Pérdida de paquetes en el aire | Retransmisiones TCP de 0-0.16 % |
| Un router en particular | Tres routers distintos, techo parecido |
| Backhaul inalámbrico o malla | Un solo router, PC por cable |

**Configuración usada en los ASUS:** 2.4 GHz con SSID propio, Smart Connect apagado, 20 MHz, canal fijo, modo inalámbrico Auto, Wi-Fi 6 apagado en 2.4 GHz, WPA2-Personal, PMF apagado, WMM encendido, Roaming Assistant/TWT/OFDMA/MU-MIMO apagados, QoS apagado.

---

## 7. Hipótesis abiertas (lo que nos gustaría que el experto valore)

1. **Ventana TCP chica en el ventilador + RTT que crece con la carga.** Si el F56 anuncia una ventana de pocos KB (típico de stacks embebidos como lwIP), el rendimiento por equipo queda en ≈ ventana / RTT. Con 5 equipos el RTT pasa de ~5 a ~50 ms y cada uno recibe menos. Pero eso no explica del todo por qué el **total** es constante.
2. **Ineficiencia de aire:** sin agregación A-MPDU hacia el cliente o con tramas chicas (MSS reducido), cada byte útil consumiría mucho tiempo de aire. ~6 Mbit/s útiles sobre un enlace de 72 Mbps parece demasiado bajo incluso así.
3. **Ahorro de energía del cliente** (modem sleep, DTIM): el AP retiene tramas hasta que el cliente despierta. Con varios equipos, se serializa. (En reposo el ping es de 3-10 ms, lo que va en contra.)
4. **Tráfico de fondo de los propios ventiladores:** los F56 tienen funciones de "Sync group" y hotspot propio. Si emiten multicast/broadcast o mantienen un AP propio en el mismo canal, ese tráfico iría a tasa básica y consumiría aire. No lo hemos capturado.
5. **Límite de CPU o decodificación del ventilador** que afecta el ritmo de ACK. No explica que el total sea compartido.

---

## 8. Pendiente de probar

| # | Prueba | Qué responde |
|---|---|---|
| 1 | **TP-Link BE19000** con la misma prueba (MLO apagado, 2.4 GHz b/g/n, 20 MHz, WPA2) | Si algún router rompe el techo de ~6 Mbit/s |
| 2 | **Captura de paquetes** (Wireshark en la PC, idealmente también un sniffer en monitor mode en el canal) | Ventana TCP y MSS que anuncia el F56, agregación, retransmisiones en el aire, tráfico multicast/broadcast de los ventiladores, ocupación del canal |
| 3 | **No reenviar cuadros idénticos** (partes negras), con un refresco cada ~0.5 s | Ahorro grande con contenido oscuro; hay que ver si el F56 muestra barras cuando pasa tiempo sin cuadros |
| 4 | **Huffman optimizado solo en cuadros con contenido** (negros con tablas estándar) | Recuperar ~30 % si las barras vienen solo de cuadros negros |
| 5 | **JPEG de menor resolución** (p. ej. 480×480) | Si el F56 escala; reduciría el mínimo y el tamaño ~40-50 % |
| 6 | Bajar a **15 fps** | Más presupuesto por cuadro |
| 7 | **Dos routers / dos canales** (p. ej. 3 ventiladores en el canal 1 y 2 en el 11) | Si el límite es por canal o por radio: duplicaría el total |
| 8 | **Prueba larga** de 30-60 min y en el lugar del evento (con público y otras redes) | Estabilidad real |
| 9 | Desactivar hotspot y "Sync group" en los F56 | Si los ventiladores generan tráfico propio que consume aire |

---

## 9. Preguntas concretas para el experto

1. Con **5 clientes 802.11n 1×1 a 72 Mbps, 0 % de pérdida**, ¿qué explica un **agregado fijo de ~6 Mbit/s** en tres routers distintos? ¿Qué mirarías primero en una captura?
2. ¿Vale la pena **repartir los ventiladores en dos canales o dos APs** (prueba 7)? ¿O el límite está en otra parte?
3. Si el cliente anuncia una ventana TCP chica, ¿hay algo **del lado del emisor o del AP** que ayude (TCP_NODELAY, tamaño de envío, WMM/AC_VI para ese tráfico, deshabilitar agregación, ajustes de beacon/DTIM)?
4. ¿Algún ajuste del AP específico para **clientes IoT 2.4 GHz con flujo continuo** (tasas mínimas/básicas, multicast-to-unicast, desactivar 802.11b, etc.)?
5. Para el evento (venue con público): ¿recomiendas **AP profesional** con control de canal y potencia, y cuántos ventiladores por radio?

---

## 10. Actualización: Ruijie RAP73 Pro

Con un AP enterprise **Ruijie RAP73 Pro** (cable a la UniFi, canal 6, 20 MHz):

| Router | Capacidad total con 5 F56 | fps promedio por ventilador (contenido holo) |
|---|---|---|
| UniFi | ~5.5 Mbit/s | 12.8 |
| **Ruijie RAP73 Pro** | **~8.5 Mbit/s** | **17.4** (presupuesto de 8 Mbit/s en SpinBridge) |

- A 40 MHz fue peor (12.2 fps): los F56 no aprovechan 40 MHz en 2.4 GHz.
- El ventilador más débil (1-1) negocia ~39 Mbps aun con buena señal (-53 dB); en los demás, 52-65 Mbps.
