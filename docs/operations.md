# Vela auf dem Heimserver

Vela 1.0.0 wird unter `https://vela.home.arpa/` ohne Anmeldung betrieben. Caddy lässt ausschließlich den tatsächlichen Client aus `192.168.1.0/24` zu. Übermittelte Forwarding-Header erweitern diese Freigabe nicht. API und Worker veröffentlichen keine Hostports. Die API prüft erlaubten Host und Browser-Origin; fremde oder fehlende Origins dürfen keine Aufträge ändern.

## Speicher

Downloads landen in `\\unas.home.arpa\Unprotected\Vela`. Die fehlende Redundanz ist vom Nutzer bewusst gewählt; Vela richtet keine Medienbackups ein.

Die vorhandene native UNAS-Freigabe Unprotected wird ausschließlich für `192.168.1.175` zusätzlich per NFS3 mit synchronen Schreibvorgängen und Root-/All-Squash bereitgestellt. Ihr nativer Eigentümer bleibt erhalten. Der Host mountet sie unter `/srv/vela/library`; die Container sehen ausschließlich den eigenen Unterordner Vela unter `/downloads`. Der Marker `.vela-volume` prüft die Speicheridentität.

SQLite liegt auf der lokalen SSD unter `/srv/vela/state/vela.sqlite3`. Das Arbeitsvolume `/srv/vela/work` ist ein eigenes 16-GiB-ext4-Dateisystem in `/srv/vela/work.ext4`. Es begrenzt auch bei fehlerhaften Größenangaben den temporären Speicherverbrauch. Der Worker hält zusätzlich 2 GiB Reserve frei.

NFS verwendet `hard`: Bei einem NAS-Ausfall können laufende Dateizugriffe bis zur Rückkehr warten. Die getrennte API kann währenddessen weiterhin Auftragsinformationen aus SQLite liefern. Ein Worker-Heartbeat wird dann gegebenenfalls veraltet angezeigt. Es entsteht kein Ersatzbestand auf der Systemplatte.

## Dienste und Netzwerk

| Dienst | Aufgabe |
| --- | --- |
| `vela-api.service` | Oberfläche und API, höchstens 256 MiB RAM |
| `vela-worker.service` | Ein Download und eine Vorschau gleichzeitig, höchstens 1 GiB RAM / eine CPU |
| `vela-browser.service` | Instagram-Anmeldung und Bestätigungsfenster, höchstens 768 MiB RAM / 0,75 CPU |
| `vela-network.service` | Eigene Docker-Netze und atomar gesetzte IPv4-Egress-Regeln |
| `srv-vela-library.mount` | UNAS-Mount |
| `srv-vela-work.mount` | Begrenztes lokales Arbeitsvolume |

Die drei Appdienste starten unabhängig im Vordergrund über Compose; Docker verwendet `restart: no`. systemd koordiniert Mounts, Firewall und Wiederanlauf. Worker und Browserdienst können neu starten, ohne die API anzuhalten. Ein vollständiger Host- oder Docker-Neustart wird nicht allein aus diesen Konfigurationen als praktisch getestet abgeleitet.

Das API-Netz `vela_proxy` ist intern (`172.29.50.0/28`); Caddy ist persistent daran angebunden. Worker und Browserdienst verwenden ausschließlich `vela_worker` (`172.29.51.0/28`) ohne IPv6. Die eigenen Chains `VELA-EGRESS` und `VELA-HOST` beschränken neue ausgehende Verbindungen auf öffentliche HTTP-/HTTPS-Ziele. Bestehende fremde Regeln bleiben erhalten. Die API steuert den Browserdienst ausschließlich über `/srv/vela/browser-control/control.sock`, ohne TCP-Port oder Zugriff auf sein Profil und seinen Zugangsspeicher.

```sh
systemctl status vela-api.service vela-worker.service vela-browser.service
journalctl -u vela-api.service -u vela-worker.service -u vela-browser.service
systemctl restart vela-worker.service
systemctl stop vela-api.service vela-worker.service vela-browser.service
systemctl start vela-browser.service vela-api.service vela-worker.service
```

Vor Start prüft `deploy/start_guard.py` Mountquelle, Marker und erforderliche Firewallregeln. Die aktuelle Quelle ist über `/opt/vela/current` erreichbar. Produktive Daten liegen getrennt davon unter `/srv/vela`.

## Sitzungen für Quellen

Unter **Server → Sitzungen für Quellen** können vom Nutzer bereitgestellte Netscape-Cookie-Dateien für Instagram, YouTube, TikTok, Facebook und X hinterlegt werden. Nur die Domains der gewählten Quelle werden übernommen; andere Cookies und Kommentare werden verworfen. Eine Datei ist auf 256 KiB begrenzt. Die [offizielle Exportanleitung](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) beschreibt das Format.

Dateien liegen mit Modus `0600` unter `/srv/vela/credentials` (Verzeichnis `0700`). Die API erhält diesen Bereich schreibbar, der Worker ausschließlich lesend. Er verwendet pro Auftrag eine eigene private Kopie, die bei Ende, Fehler, Abbruch und Wiederanlauf entfernt wird. Cookie-Inhalte werden nicht in API-Antworten, Jobmetadaten oder Logs ausgegeben. Das Entfernen einer hinterlegten Sitzung wirkt auf neu beginnende Abrufe.

Die Zuordnung erfolgt anhand der Quelladresse, einschließlich passender Kurzlinks. Google-Cookies können zur YouTube-Sitzung gehören; beliebige Google-Seiten werden dadurch nicht als YouTube-Quelle behandelt. Eine hinterlegte Datei bestätigt ihr Format, nicht die Annahme durch die Plattform. Für Anmeldung, Zugriffsverweigerung, Abruflimits, Regions- oder DRM-Beschränkungen gibt es getrennte Fehlerhinweise. Vela nutzt keine externen Downloadportale als Fallback.

## Instagram-Anmeldung

Unter **Server → Sitzungen für Quellen → Instagram** werden Benutzername und Passwort direkt in Vela hinterlegt. Der Browserdienst speichert sie mit AES-GCM verschlüsselt unter `/srv/vela/browser-secrets`; der lokale Schlüssel liegt daneben, Dateien mit `0600`, Verzeichnis mit `0700`. Dies trennt Geheimnisse von der Auftragsdatenbank, ersetzt aber keinen Schutz vor einem Administrator des Heimservers. Nur der Browsercontainer erhält diesen Bereich und `/srv/vela/browser-profile` schreibbar. Persönliche PC-Browserprofile werden nicht verwendet.

Die Anmeldung verwendet Chromium mit Playwright in einem eigenen Profil. Vorhandene Sitzungen werden wiederverwendet, neue Sitzungen ausschließlich für Instagram nach `/srv/vela/credentials/instagram.cookies.txt` übernommen. Ein lokaler SQLite-Steuerzustand enthält nur Status, begrenzte Erneuerungsanforderungen und Generationsnummern. Keine Zugangsdaten, Cookie-Werte, Seiteninhalte oder Screenshots werden darin gespeichert.

Nach einem harten Containerabbruch prüft der Dienst vor dem Browserstart verwaiste Chromium-Profilsperren. Er benötigt dafür seinen exklusiven Profilbesitz und prüft auf noch laufende Prozesse. Ausschließlich die drei bekannten Chromium-Sperrlinks werden entfernt; ihre Ziele und übrige Profildaten bleiben erhalten. Der Dienst beendet dafür keine fremden Prozesse. Unbekannte Sperrdateien oder ein erkennbar aktiver Browser führen zu einer Fehlermeldung.

Verlangt Instagram einen Code oder eine Bestätigung, öffnet der Nutzer das Anmeldefenster in Vela. Das Bild wird ohne Zwischenspeicherung übertragen; Klicks, Text, begrenzte Tasten und Scrollen steuern nur dieses Fenster. Passwortfelder sind maskiert. Es gibt keinen frei adressierbaren Browser und keinen externen Download-Dienst. Nach einer erfolgreichen Anmeldung werden wartende Instagram-Aufträge automatisch wieder eingereiht; ein fehlgeschlagener Abruf führt je Nutzer-Versuch höchstens zu einer automatischen Erneuerung. Andere Aufträge können währenddessen weiterlaufen.

**Zugang entfernen** löscht die hinterlegten Zugangsdaten, das eigene Browserprofil und die Instagram-Sitzung. Bereits begonnene Downloader können ihre private Arbeitskopie bis zum Ende verwenden. Eine erneute Anmeldung und ein erfolgreicher Download sind erst mit einem vom Nutzer bereitgestellten, tatsächlich berechtigten Konto praktisch prüfbar. Instagram kann Anmeldungen und Videozugriffe weiterhin verweigern.

## Build und Auslieferung

Die Runtime verwendet Python 3.13.14, separat gebaute SQLite 3.51.3, yt-dlp 2026.10.26, EJS 0.8.0, Deno 2.9.8 und FFmpeg aus Debian. Python-Abhängigkeiten sind in `requirements.lock`, Browser-Abhängigkeiten in `frontend/package-lock.json` festgeschrieben.

```sh
docker build --target test -t vela-test:1.0.0 .
docker build --target worker -t vela-worker:1.0.0 .
docker build --target api -t vela-api:1.0.0 .
docker build --target browser -t vela-browser:1.0.0 .
```

Ein optionales Linux-x86_64-Buildpaket enthält die gebauten Containerimages, Quellen, Hashmanifest und `install.sh`. Das Installationsskript ist für den bereits vorbereiteten Heimserver bestimmt; es richtet keine fremden Server oder NAS-Zugänge ein. Es prüft Payload und Imageidentitäten vor dem Wechsel. Vela wird direkt auf dem Heimserver bereitgestellt; Orbit-Veröffentlichungen erfolgen ausschließlich auf ausdrücklichen Nutzerauftrag. Es gibt keinen automatischen In-App-Updater; die Weboberfläche wird mit dem Serverrelease aktualisiert.

`deploy/unas_native.py`, `prepare_host.py`, `network.py`, `caddy.py` und `install_units.py` bilden den vorbereiteten Einrichtungsweg ab. Änderungen an gemeinsamem DNS, Caddy oder NEXUS müssen ihren aktuellen Zustand erhalten. Die privaten Vorherkopien auf dem Server sind technische Rückwege und gehören nicht ins Quellpaket.

## Funktionsgrenzen

Die erste Version unterstützt einzelne Videos, Audio, begrenzte Playlistauflösung und Auswahl von Einträgen, Vorschau, Queue, Abbruch, Wiederholen, Verlauf und Dateidownload sowie die Instagram-Anmeldung mit eigener Sitzung. Abonnements und allgemeine Browserintegrationen sind noch nicht enthalten. Plattformbedingte Zugangsanforderungen können weiterhin Eingriffe und spätere Engine-Updates erfordern.
