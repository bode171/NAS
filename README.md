# NAS – Bild-Zuspieler für grandMA3

Windows-App, die Bilder im Vollbild auf dem Beamer zeigt und von der grandMA3 light per **sACN** oder **Art-Net** gesteuert wird.
Jedes Bild ist ein Dimmer-Kanal. Der Kanalwert bestimmt die Sichtbarkeit, und die Überblendungen kommen aus den Cue-Fade-Zeiten.

Beispiel: FID 9001 auf 7.001 = Bild 1, FID 9002 auf 7.002 = Bild 2 und so weiter.
Das **Universum ist frei wählbar**, die 7 ist nur ein Beispiel (siehe Abschnitt 4).

---

## 1. EXE über GitHub bauen (ohne Python auf dem Laptop)

1. Neues Repository auf GitHub anlegen, z. B. `bild-zuspieler`. Es kann privat sein.
2. **`main.py`**, **`requirements.txt`** und **`README.md`** hochladen: *Add file → Upload files*.
3. Den Workflow anlegen: *Add file → Create new file*
   - Als Dateiname genau `.github/workflows/build.yml` eintippen. Die Schrägstriche legen die Ordner automatisch an.
   - Den Inhalt von `build.yml` hineinkopieren und auf *Commit changes* tippen.
4. Den Reiter **Actions** öffnen. Der Build startet von allein und dauert etwa 3–5 Minuten.
   Manuell starten geht über *Windows-EXE bauen → Run workflow*.
5. Wenn der Build grün ist: den Lauf anklicken und unten bei **Artifacts** `NAS-Windows` herunterladen.
   Das ist eine ZIP-Datei, in der die `NAS.exe` liegt.

Bei jedem neuen Hochladen von `main.py` wird automatisch eine neue EXE gebaut.

> Beim Build läuft ein Selbsttest mit: Art-Net/sACN-Empfang, Patch, Überblendung und Speichern.
> Wenn ein Schritt rot ist, kannst du mir das Log aus dem Actions-Lauf schicken.

## 2. Erster Start am Laptop

- Die EXE in einen eigenen Ordner legen, z. B. `C:\NAS\` oder auf den Desktop.
  Daneben legt die App den Ordner **`NAS_Daten`** an. Darin liegen die Show (`show.json`) und Kopien aller Bilder.
  Für einen anderen Laptop einfach die EXE samt diesem Ordner kopieren.
- **Windows SmartScreen** („Der Computer wurde geschützt“): auf *Weitere Informationen → Trotzdem ausführen* klicken.
- **Firewall-Abfrage**: Zugriff erlauben, mindestens für *private Netzwerke*. Sonst kommen keine DMX-Daten an.
- Der erste Start einer Onefile-EXE dauert ein paar Sekunden, weil sie sich erst entpackt.

## 3. Netzwerk

- Laptop und grandMA3 light müssen im **selben Netz** sein, z. B. beide `2.x.x.x` oder beide `192.168.1.x`.
- In der App unter **Einstellungen → Netzwerk-Interface** die Netzwerkkarte wählen, an der die Konsole hängt (für sACN wichtig).
- Zur Kontrolle dient der **DMX-Monitor**. Dort siehst du, welche Universen ankommen, von welcher IP und wie viele Pakete pro Sekunde.

## 4. grandMA3 einrichten (2.5)

**Freies Universum finden**
- In der grandMA3 im Patch nachsehen, welche Universen schon belegt sind. Die Adressauswahl beim Patchen zeigt die Belegung pro Universum an.
  Ein freies Universum ist eines, auf dem kein echtes Gerät hängt.
- In der App im **Patch** oben bei *Universum für die Bilder* die Nummer eintragen. Alle Bilder wandern mit, die Adressen bleiben gleich.
- Oder **Universum lernen**: Den Dialog öffnen, dann an der Konsole den ersten Bild-Dimmer auf Full ziehen.
  Die App erkennt Universum und Kanal und patcht alle Bilder fortlaufend ab dort.
  Dafür muss das Universum an der Konsole schon unter *DMX Protocols* ausgegeben werden.

**Patch**
- *Menu → Patch → Insert New Fixture*: Fixture Type **Dimmer** (Generic), Anzahl = Anzahl der Bilder, **FID 9001**, Patch auf dein freies Universum, Adresse 1. Bei Universum 7 also 7.001.
- Die Namen können gleich wie in der App sein, z. B. „Logo“ oder „Pause“.

**DMX-Ausgabe**
- *Menu → DMX Protocols*, dann sACN oder Art-Net wählen, aktivieren und eine Zeile anlegen:
  - Mode **Output**, Local Universe = dein Universum (im Beispiel 7)
  - **sACN**: dieselbe Nummer, Multicast. Alternativ Unicast mit der IP des Laptops.
  - **Art-Net**: eins weniger (bei 7 also **6**), weil Art-Net ab 0 zählt. Broadcast oder Unicast an den Laptop.
- Wenn die Zählung nicht passt: In der App unter *Einstellungen → Art-Net-Zählung* den Wert ändern.
  Der DMX-Monitor zeigt an, unter welcher Nummer die Daten ankommen.

**Cues**
- Cue 1: 9001 auf Full → Bild 1 blendet in der Cue-Fade-Zeit ein.
- Cue 2: 9001 auf 0, 9002 auf Full, Fade 3 s → 3-Sekunden-Crossfade von Bild 1 auf Bild 2.
- Auch Executor-Fader und Layout-Buttons funktionieren, weil es ganz normale Dimmer sind.

**Vorschaubilder auf der Konsole**
- In der App unter *Patch → Vorschaubilder für grandMA3 exportieren* den USB-Stick wählen.
  Die App legt die Bilder in `grandMA3/gma3_library/media/images` ab. Die Dateinamen beginnen mit der FID, z. B. `9001_Logo.png`.
- An der Konsole die Bilder in den **Images**-Pool importieren und daraus **Appearances** bauen.
  Diese Appearances dann den Layout-Elementen oder Sequenzen zuweisen, dann zeigen die Touch-Buttons das Bild.

## 5. Bedienung der App

| Bereich | Funktion |
|---|---|
| **Image Pool** | Bilder als Kacheln mit Nummer, FID, Adresse und Live-Pegel. `+` oder Drag & Drop zum Hinzufügen. Doppelklick = **Test** (lokal 100 %, ohne Konsole). Rechtsklick = Umbenennen, Ersetzen, Entfernen. |
| **Patch** | Tabelle: FID, Name, Universum, Adresse, Fade-Zeiten. Doppelt belegte Adressen werden rot markiert. Auto-Patch belegt ab Start-FID und Startadresse neu. |
| **DMX-Monitor** | 512 Kanäle des gewählten Universums, gepatchte Kanäle blau markiert. |
| **Einstellungen** | Protokoll, Netzwerkkarte, Beamer-Bildschirm, Einpassen/Füllen, Mischmodus, Standard-Fades, optionaler Master-Kanal. |
| **F2** | Ausgabe auf dem Beamer an/aus |
| **F3** | Blackout (lokal in der App) |

**Mischmodus**
- *Mischen* (Standard): Bilder werden addiert. Ein Crossfade 100→0 und 0→100 läuft ohne dunkles Loch in der Mitte.
- *Überdecken*: Die Bilder liegen übereinander, die höhere FID liegt oben. Das ist sinnvoll für Bilder mit transparenten Bereichen (PNG), z. B. ein Logo über einem Hintergrund.

**Fade-Zeiten in der App**
- Bei 0 s folgt die App der Konsole direkt. Das ist der Normalfall, die Fades macht die Konsole.
- Bei Werten über 0 blendet die App zusätzlich selbst weich, auch wenn die Konsole hart schaltet. Das lässt sich pro Bild im Patch einstellen.

**Wenn das Signal ausfällt**, bleibt das letzte Bild stehen. Es wird also nicht schwarz.

## Geplant
- Videos (Start/Stopp, Schleife, Ton ans Mischpult)
