# FramePilot – Blender Camera Director via MCP (Codex)

Ein kleines MCP-Lernprojekt: Du benutzt **Codex CLI oder die lokale Codex IDE-Erweiterung**, um eine `.blend`-Datei zu untersuchen, die Kamera in einer **Arbeitskopie** zu verändern und ein Preview-Rendering zurückzubekommen.

## Architektur

```text
Codex CLI / lokale Codex IDE
        ↕ MCP über stdio
TypeScript MCP-Server: src/server.ts
        ↕ Blender-Subprozess
Blender Python API: scripts/blender_bridge.py
        ↕
scene.blend → runtime/session.blend + runtime/preview.png
```

Der Server führt Blender im Hintergrund pro Tool-Aufruf aus. Die Originalszene wird nicht überschrieben; die Änderungen stehen in `runtime/session.blend`. Es ist **noch keine Live-Verbindung** zur geöffneten Blender-Oberfläche.

## Voraussetzungen

- macOS mit Blender (4.x oder 5.x), standardmäßig `/Applications/Blender.app/Contents/MacOS/Blender`
- Node.js 20+, npm
- Codex CLI oder die lokale Codex IDE-Erweiterung; **nicht Codex Cloud**, da diese keinen direkten Zugriff auf deine lokale Blender-Installation hat

## Erste Schritte

```bash
cd framepilot
npm install
npm run check
npm run demo
npm run setup:codex
```

`npm run demo` erstellt eine kleine dunkle Szene mit Säulen und schwebendem Auge (`scene.blend`). `npm run setup:codex` legt `.codex/config.toml` mit den absoluten Pfaden auf deinem Rechner an. Diese Datei ist gitignored, weil sie maschinenspezifische Pfade enthält. Eine bestehende Konfiguration wird nicht überschrieben.

**Blender an anderer Stelle installiert?** Dann setze denselben abweichenden Pfad bei beiden Befehlen:

```bash
BLENDER_PATH="/pfad/zur/Blender-App/Contents/MacOS/Blender" npm run demo
BLENDER_PATH="/pfad/zur/Blender-App/Contents/MacOS/Blender" npm run setup:codex
```

Öffne **den gesamten Projektordner** in der lokalen Codex CLI (`codex`) oder Codex IDE. **Vertraue dem Projekt**, sonst überspringt Codex `.codex/config.toml`. In der CLI zeigt `/mcp` aktive Server. Nach Änderungen der MCP-Konfiguration die Codex-Sitzung bzw. IDE-Erweiterung neu starten.

Alternativ: MCP-Server global per `codex mcp add` registrieren, wenn du ihn aus anderen Projektordnern verwenden willst. Die mitgelieferte Variante ist absichtlich projektlokal.

## Tools

- `inspect_scene()` – Szene, Objekte, Kamera, Render-Engine und Positionen lesen
- `set_camera(position, target, lens_mm)` – Position, Zielpunkt und Brennweite setzen; Arbeitskopie speichern
- `render_preview()` – 480×270 PNG rendern und als MCP-Bild-Ergebnis zurückgeben

## Beispielprompt

> Verwende FramePilot. Inspiziere zuerst die Szene. Positioniere die Kamera so, dass das rote Auge bedrohlich und dominant wirkt. Rendere ein Preview. Erkläre kurz die Komposition. Ändere danach nichts mehr ohne meine Freigabe.

Das visuelle Interpretieren des Bildes hängt davon ab, ob der lokale Codex-Client MCP-Bildantworten entsprechend weiterreicht. In jedem Fall landet die Datei unter `runtime/preview.png`.

## Eigene .blend nutzen

Lege eine eigene Datei als `scene.blend` im Projektordner ab oder setze `FRAMEPILOT_SCENE` beim `npm run setup:codex`. Wenn schon eine Arbeitskopie existiert, zuerst `npm run reset` ausführen.

## MCP bewusst lernen

1. Lies `src/server.ts`: `registerTool` + `inputSchema` definieren den Tool-Vertrag.
2. Lies `scripts/blender_bridge.py`: Python/bpy setzt den Tool-Aufruf in Blender um.
3. Baue ein viertes Tool `list_cameras` oder `set_focus_distance`.
4. Teste Fehlerfälle und lerne, warum schreibende Aktionen eine Freigabe verdienen.

## Troubleshooting

- **Kein FramePilot in `/mcp`**: Prüfe, ob Codex **lokal**, im Projektordner und mit Projektvertrauen gestartet ist; starte die Sitzung neu.
- **Blender-Prozess startet nicht**: `BLENDER_PATH` auf die echte Blender-Binärdatei setzen; `npm run setup:codex` erzeugt die Konfiguration erneut nur nach explizitem Löschen der generierten Datei.
- **Real Water 2 / Fluid Painter wirft beim Beenden `missing bl_rna`**: FramePilot startet Blender nun mit `--factory-startup`, damit normale Benutzer-Add-ons nicht geladen werden. Die Add-ons bleiben in der regulären Blender-Oberfläche unverändert. Falls nach `npm run demo` die Datei `scene.blend` existiert und „Demo-Szene erstellt“ ausgegeben wurde, war der Schritt erfolgreich.
- **`scene.blend` fehlt**: `npm run demo` ausführen oder eine eigene Datei ablegen.
- **Große Szene rendert langsam**: Blender läuft einmal pro Aufruf; standardmäßig bis 180 Sekunden pro Operation.

**Sicherheit:** Verwende nur vertrauenswürdige `.blend`-Dateien. Blender kann Skripte und Treiber ausführen. Die lokale Codex-Instanz braucht Zugriff auf die Blender-Binärdatei.
