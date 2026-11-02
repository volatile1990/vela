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
| `vela-browser.service` | Instagram-Anmeldedienst und Bestätigungsfenster, höchstens 768 MiB RAM / 0,75 CPU |
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

Die Anmeldung verwendet primär Instaloader 4.16 mit direktem Zugriff auf Instagram. Die gespeicherte Sitzung wird vor einer erneuten Passwortanmeldung geprüft. Auch ein noch nicht bestätigter Anmeldezustand wird bereits verschlüsselt als JSON im bestehenden Geheimnisspeicher abgelegt. Eine Sicherheitsabfrage, ein Netzfehler oder eine Abrufbegrenzung werden nicht als Erlaubnis für wiederholte Passwortversuche behandelt. Instaloaders Pickle-Dateifunktionen werden nicht verwendet. Erfolgreich geprüfte Cookies werden ausschließlich für Instagram nach `/srv/vela/credentials/instagram.cookies.txt` übernommen. Der lokale SQLite-Steuerzustand enthält weiterhin nur Status, begrenzte Erneuerungsanforderungen und Generationsnummern.

Eine von Instaloader erfolgreich abgeschlossene Passwortanmeldung mit gegebenenfalls bestätigtem TOTP und neuem Sitzungscookie wird bereits vor der zusätzlichen GraphQL-Profilprüfung als nachgewiesene Anmeldung gespeichert. Meldet ausschließlich diese Prüfung `feedback_required`, kann Vela die nachgewiesene Sitzung weiterverwenden. Das gilt auch für eine zuvor geprüfte Sitzung. Ohne diesen Nachweis ist Feedback ein Fehler und keine Aufforderung zur Handybestätigung; echte Challenges, Checkpoints, eine ausdrückliche Ablehnung der Anmeldung und abweichende Kontoidentitäten sperren die Freigabe weiterhin.

Verlangt Instagram eine zusätzliche Freigabe und liefert einen geeigneten Bestätigungsweg, öffnet Vela einen nicht persistenten Chromium-Kontext mit den zugehörigen Sitzungscookies. Dieser Kontext ist von dem bisherigen Vela-Browserprofil getrennt. Der Browser dient der Bestätigung durch den Nutzer; er wiederholt die API-Anmeldung nicht automatisch mit einem weiteren Passwort. Ohne geeigneten Browserweg verweist Vela auf die Instagram-App. Danach prüft „Erneut versuchen“ zuerst den gespeicherten Zustand.

Optional kann unter Instagram ein Authenticator-Einrichtungsschlüssel hinterlegt werden. Vela akzeptiert Base32 und `otpauth://totp` für SHA-1, sechs Ziffern und 30 Sekunden. Ein QR-Bild wird ausschließlich im Browser dekodiert; übertragen wird nur sein Inhalt. Der Google-Authenticator-Import akzeptiert ausschließlich ein einzelnes Instagram-Konto im unterstützten `otpauth-migration`-Format. Mehrere Konten, mehrteilige Exporte und unbekannte Formate werden abgewiesen. [Google beschreibt den gezielten Export ausgewählter Einträge](https://support.google.com/accounts/answer/1066447?hl=de). Den Export oder Einrichtungsschlüssel niemals an einen öffentlichen QR-Decoder oder in einen Chat senden.

Der TOTP-Schlüssel liegt gemeinsam mit dem Passwort im bestehenden AES-GCM-Zugangsspeicher, niemals im öffentlichen Status, in der Steuerdatenbank oder in Logs. Der Status enthält ausschließlich `totp_configured`. Speichern und Entfernen dieses Schlüssels starten keinen Login und erhalten Zugang, Profil und bestehende Quellsitzung. Die API verwendet dafür ausschließlich die feste interne Unix-Socket-Route `/totp`. Beim Wechsel des Instagram-Kontos wird der bisherige TOTP-Schlüssel entfernt. Bei Bedarf wird pro Anmeldeanforderung höchstens ein Code automatisch eingetragen; das Versuchsbudget überlebt Dienstneustarts. Kurz vor dem Ablauf eines Zeitfensters wird auf einen frischen Code gewartet.

Fordert die API einen App-Code, erzeugt Vela ihn aus dem hinterlegten Einrichtungsschlüssel und sendet ihn einmal. Andere Codearten und zusätzliche Gerätefreigaben bleiben eine Nutzeraufgabe. Verlangt Instagram eine Browserbestätigung, öffnet der Nutzer das Anmeldefenster in Vela. Das Bild wird ohne Zwischenspeicherung übertragen; Klicks, Text, begrenzte Tasten und Scrollen steuern nur dieses Fenster. Eingabefelder sind maskiert. Es gibt keinen frei adressierbaren Browser und keinen externen Download-Dienst. Nach einer erfolgreichen Anmeldung werden wartende Instagram-Aufträge automatisch wieder eingereiht; ein fehlgeschlagener Abruf führt je Nutzer-Versuch höchstens zu einer automatischen Erneuerung. Andere Aufträge können währenddessen weiterlaufen.

**Zugang entfernen** löscht die hinterlegten Zugangsdaten, den API-Sitzungszustand, das bisherige eigene Browserprofil und die Instagram-Quellsitzung. Änderungen am Authenticator-Schlüssel erhalten den übrigen Zustand. Bereits begonnene Downloader können ihre private Arbeitskopie bis zum Ende verwenden. Instagram kann Anmeldungen und Videozugriffe weiterhin verweigern.

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
