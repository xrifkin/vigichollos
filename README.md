# Vigilante de chollos (Chollometro → ntfy)

Programa pequeño que, cada 10 minutos, lee las ofertas nuevas de Chollometro y te avisa
al móvil (con la app ntfy) cuando alguna parece un error de precio.

## Qué hay en cada archivo

| Archivo | Para qué sirve |
|---|---|
| `monitor.py` | El programa: lee el feed, aplica las reglas y envía los avisos |
| `config.json` | **Lo único que querrás tocar**: palabras clave, descuento mínimo, feeds |
| `state.json` | La memoria del programa (ofertas ya vistas). Se actualiza solo |
| `.github/workflows/vigilar.yml` | Le dice a GitHub que ejecute el programa cada 10 minutos |
| `tests/feed_ejemplo.xml` | Feed inventado para probar el programa sin internet |

## Reglas actuales (en `config.json`)

Se avisa si la oferta nueva cumple **cualquiera** de estas condiciones:

1. Menciona una frase como «error de precio», «errorazo», «gazapo»… (aviso con prioridad alta).
2. Tiene un descuento del 70 % o más (escrito en el título o calculado con un «antes X €» explícito).
3. Su precio es de 1 € o menos (salvo muestras gratis, cupones, sorteos y similares).

## Secreto necesario en GitHub

- `NTFY_TOPIC`: el nombre de tu tema de ntfy (el mismo que pongas en la app del móvil).
  Se guarda en *Settings → Secrets and variables → Actions*, nunca dentro de los archivos.

## Primera ejecución recomendada

1. Lanzar a mano el flujo en modo **test** → debe llegarte una notificación al móvil.
2. Lanzarlo en modo **diagnose** → en el registro verás qué datos trae realmente el feed
   de Chollometro y qué ofertas habrían disparado un aviso.
3. Ajustar `config.json` con lo que veas y dejar que siga solo en modo **run**.

## Si dejas de recibir avisos

- Mira la pestaña **Actions** del repositorio: cada ejecución aparece ahí con su registro.
- El programa también te avisa por ntfy si lleva 6 ejecuciones seguidas sin poder leer el feed.
- GitHub puede pausar las ejecuciones programadas de un repositorio sin actividad durante
  mucho tiempo; si pasa, basta con reactivarlas desde la pestaña Actions.
