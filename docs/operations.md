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
| `vela-network.service` | Eigene Docker-Netze und atomar gesetzte IPv4-Egress-Regeln |
| `srv-vela-library.mount` | UNAS-Mount |
| `srv-vela-work.mount` | Begrenztes lokales Arbeitsvolume |

Die beiden Appdienste starten unabhängig im Vordergrund über Compose; Docker verwendet `restart: no`. systemd koordiniert Mounts, Firewall und Wiederanlauf. Der Worker kann neu starten, ohne die API anzuhalten. Ein vollständiger Host- oder Docker-Neustart wird nicht allein aus diesen Konfigurationen als praktisch getestet abgeleitet.

Das API-Netz `vela_proxy` ist intern (`172.29.50.0/28`); Caddy ist persistent daran angebunden. Der Worker verwendet ausschließlich `vela_worker` (`172.29.51.0/28`) ohne IPv6. Die eigenen Chains `VELA-EGRESS` und `VELA-HOST` beschränken neue ausgehende Verbindungen auf öffentliche HTTP-/HTTPS-Ziele. Bestehende fremde Regeln bleiben erhalten.

```sh
systemctl status vela-api.service vela-worker.service
journalctl -u vela-api.service -u vela-worker.service
systemctl restart vela-worker.service
systemctl stop vela-api.service vela-worker.service
systemctl start vela-api.service vela-worker.service
```

Vor Start prüft `deploy/start_guard.py` Mountquelle, Marker und erforderliche Firewallregeln. Die aktuelle Quelle ist über `/opt/vela/current` erreichbar. Produktive Daten liegen getrennt davon unter `/srv/vela`.

## Build und Auslieferung

Die Runtime verwendet Python 3.13.14, separat gebaute SQLite 3.51.3, yt-dlp 2026.10.26, EJS 0.8.0, Deno 2.9.8 und FFmpeg aus Debian. Python-Abhängigkeiten sind in `requirements.lock`, Browser-Abhängigkeiten in `frontend/package-lock.json` festgeschrieben.

```sh
docker build --target test -t vela-test:1.0.0 .
docker build --target worker -t vela-worker:1.0.0 .
docker build --target api -t vela-api:1.0.0 .
```

Ein optionales Linux-x86_64-Buildpaket enthält die gebauten Containerimages, Quellen, Hashmanifest und `install.sh`. Das Installationsskript ist für den bereits vorbereiteten Heimserver bestimmt; es richtet keine fremden Server oder NAS-Zugänge ein. Es prüft Payload und Imageidentitäten vor dem Wechsel. Vela wird direkt auf dem Heimserver bereitgestellt; Orbit-Veröffentlichungen erfolgen ausschließlich auf ausdrücklichen Nutzerauftrag. Es gibt keinen automatischen In-App-Updater; die Weboberfläche wird mit dem Serverrelease aktualisiert.

`deploy/unas_native.py`, `prepare_host.py`, `network.py`, `caddy.py` und `install_units.py` bilden den vorbereiteten Einrichtungsweg ab. Änderungen an gemeinsamem DNS, Caddy oder NEXUS müssen ihren aktuellen Zustand erhalten. Die privaten Vorherkopien auf dem Server sind technische Rückwege und gehören nicht ins Quellpaket.

## Funktionsgrenzen

Die erste Version unterstützt einzelne Videos, Audio, begrenzte Playlistauflösung und Auswahl von Einträgen, Vorschau, Queue, Abbruch, Wiederholen, Verlauf und Dateidownload. Abonnements und eine Verwaltung von Browsercookies sind noch nicht enthalten. Plattformbedingte Zugangsanforderungen werden als Fehler angezeigt und benötigen gegebenenfalls ein späteres Engine-Update.
