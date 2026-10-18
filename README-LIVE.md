# FramePilot Live – Ergänzung für dein bestehendes FramePilot-Projekt

**Kein neues Projekt anlegen.** Diese Dateien sind als Overlay gedacht und behalten dein angepasstes `src/server.ts` einschließlich `set_light_energy`.

## Einmalige Einrichtung (Mac + Blender 5.2)

1. ZIP **in deinem bestehenden FramePilot-Ordner entpacken**. Die neuen Dateien liegen danach unter `src/live.ts`, `scripts/enable-live.mjs`, `addons/framepilot_live.py` und `README-LIVE.md`.
2. In deinem FramePilot-Projektordner ausführen:

   ```bash
   node scripts/enable-live.mjs
   npm run check
   ```

   Vor der Änderung wird `src/server.ts.before-live.bak` erstellt. **Deine eigenen MCP-Tools bleiben erhalten.** Wenn `npm run check` eine unbekannte Action `set_light_energy` meldet, erweitere den Action-Union-Typ in `runBlender()` um `'set_light_energy'`.

3. In der **normalen Blender-App** deine `scene.blend` öffnen. Unter **Edit > Preferences > Add-ons > Dropdown > Install from Disk...** die Datei `addons/framepilot_live.py` auswählen, dann **FramePilot Live** aktivieren. Alternativ kannst du das separate `framepilot_live.py` installieren.
4. In Blender im 3D-Viewport **N** drücken → Tab **FramePilot**. Falls deine `.blend` nicht direkt im FramePilot-Projektordner liegt, im Feld **Project** den Projektordner auswählen (den, der `src/server.ts` enthält). Sonst Feld leer lassen.
5. **Start Live Bridge** klicken. Es erscheint **Connected**. Die Brücke lauscht ausschließlich auf `127.0.0.1`, nutzt einen zufälligen Port und einen geheimen Token in `runtime/framepilot-live.json`.
6. Codex **neu starten**, oder deinen LangGraph-Agenten erneut ausführen. Die bestehenden Tools wechseln automatisch zu Live, solange die Blender-Verbindung aktiv ist. **Kein erneutes `npm run demo` und kein neues `setup:codex` erforderlich.**

## Minimaler Test (geringe Limits)

In Codex nur **ein einziges Tool** aufrufen:

> Rufe genau einmal `inspect_scene` über FramePilot auf. Keine weiteren Tools, keine Wiederholungen. Antworte lediglich, ob im Ergebnis `"mode": "live"` steht.

Oder deinen sparsamen LangGraph-Agenten verwenden:

```bash
source agent/.venv/bin/activate
python agent/agent.py "Inspect the current scene once." --provider ollama
```

Beim nächsten Test `set_camera` verwenden. Im Blender-Viewport **View > Cameras > Active Camera** auswählen (oder Numpad 0), damit du die Bewegung direkt in der Kameraansicht siehst. Das Tool ändert die Szene im **laufenden Blender**, ohne die Datei zu speichern. Du entscheidest selbst, wann du `File > Save` drückst.

Mit `set_light_energy` ändert sich die Lichtstärke live, wenn du die 3D-Ansicht im **Rendered**-Modus anschaust. `render_preview` rendert weiterhin `runtime/preview.png` (480×270), was die UI für die Renderdauer blockieren kann; die ursprünglichen Render-Einstellungen werden danach wiederhergestellt.

## Betriebsarten

- **Auto (Standard):** Mit laufender Bridge → live; ohne Bridge → altes Hintergrund-Verhalten.
- **Explizit live:** `FRAMEPILOT_MODE=live python agent/agent.py ...` → Fehler, falls Blender nicht verbunden ist.
- **Altes Verhalten erzwingen:** `FRAMEPILOT_MODE=headless python agent/agent.py ...` → separate Hintergrund-Blender-Instanz und `runtime/session.blend`.

Im Terminal lässt sich `FRAMEPILOT_MODE=live` auch vor `codex` setzen, wenn du ein versehentliches Headless-Fallback vermeiden möchtest.

## Wichtige Hinweise

- **Live-Änderungen sind zunächst nur im Speicher der geöffneten Blender-Datei.** Vor dem Schließen selbst speichern. `runtime/session.blend` wird im Live-Modus **nicht** verändert.
- Blender Add-on-Button **Stop Live Bridge** trennt die Verbindung. Auch das Deaktivieren des Add-ons beendet sie.
- Nur eine Blender-Instanz pro Projekt verbinden, um Verwirrung über die aktive Szene zu vermeiden.
- **Keine beliebige Python-Code-Ausführung** über die Bridge: erlaubt sind nur `inspect`, `set_camera`, `set_light_energy`, `render`.
- Lokale Ollama-Modelle und Blender teilen sich den Arbeitsspeicher des Mac. Starte für Live-Experimente mit einem kleineren Modell.
- Blender 5.2 wurde hier nicht ausgeführt: Dateien wurden statisch geprüft, die Verbindung benötigt deinen Test in Blender.
- Sollte eine unterbrochene Blender-Sitzung eine alte `runtime/framepilot-live.json` zurücklassen, Blender Bridge neu starten; der Server fällt mit dieser Datei absichtlich nicht still auf Hintergrundmodus zurück.
