# Abnahme Vela 1.0.0

Stand 2. November 2026, Europe/Berlin. Produktiver Zugang: `https://vela.home.arpa/`, ohne Anmeldung, ausschließlich Trusted-Netz.

| Prüfung | Ergebnis |
| --- | --- |
| Linux-Tests im Runtime-Build | 61 bestanden, einschließlich POSIX-Prozessgruppen, Wiederanlauf, sicherer Dateizugriffe und tatsächlicher yt-dlp-Formatprüfungen |
| Frontendtests und Build | 12 bestanden, Production-Build erfolgreich |
| Browser | Desktop und mobile Breite 390 px geprüft, unmittelbarer Einstieg ohne Login, Vorschau tatsächlich ausgelöst |
| Originalvideo | MDN Flower, 1.128.375 Byte, erfolgreich auf UNAS gespeichert |
| Nur Audio | MDN Flower, M4A, 163.552 Byte, erfolgreich auf UNAS gespeichert |
| YouTube und Kompatibilität | Me at the zoo, MP4, 629.172 Byte, erfolgreich inklusive tatsächlicher Codecprüfung |
| Dateiintegrität | SHA-256 über Vela-Download und unabhängig direkt auf dem UNAS identisch |
| Worker-Neustart | 15 laufende API-Prüfungen erfolgreich, kein API-Ausfall |
| Trusted-Netz | Zugriff ohne Anmeldung HTTP 200; echte Docker-Bridge-Quelle auch mit gefälschten Forwarding-Headern HTTP 403 |
| Origin-Prüfung | Fehlender oder fremder Origin für Mutationen HTTP 403; Loginendpunkt entfernt, HTTP 404 |
| Netztrennung | Worker erreicht öffentliche HTTPS-Ziele; Gateway, Host und API gesperrt; IPv6-Zugriff nicht verfügbar; API-Internetzugriff gesperrt |
| Betriebszustand | Eigene Container gesund; bestehender Caddy-Container mit unveränderter Identität und Restartzähler 0 |

Die eigenen drei Testmedien und acht zugehörigen Smoke-Aufträge wurden nach Abnahme gezielt entfernt. Die Speichermarker bleiben erhalten. Eine Vorschau aus der Browserprüfung kann intern als Metadatensatz erhalten bleiben; sie ist kein gespeichertes Medium.

NAS-Ausfall, unterbrochene Finalisierung und Prozessabbrüche wurden mit isolierten Tests geprüft. Ein vollständiger Heimserver-/Docker-Neustart und ein echter produktiver NAS-Ausfall wurden nicht ausgelöst. Für die ausdrücklich ungeschützten Downloadmedien werden keine zusätzlichen Backups eingerichtet.

## Veröffentlichung

Das vorübergehend veröffentlichte Orbit-Release wurde am 2. November 2026 auf ausdrücklichen Nutzerwunsch zurückgezogen und die Kataloganwendung deaktiviert. Vela bleibt direkt unter `https://vela.home.arpa/` und über NEXUS erreichbar. Künftige Orbit-Veröffentlichungen erfolgen ausschließlich auf ausdrücklichen Auftrag.

Das geprüfte Linux-Buildartefakt bleibt technisch nachvollziehbar: `vela-1.0.0-linux-x86_64.tar.gz`, 304.009.828 Byte, SHA-256 `efaa5d99f3b50fb9b460225f41d8240feebfaa212ce07b0d48905fbe24a1a5a6`.

Die damals geprüfte Paketquelle ist Commit `4ce1fec8134d7eccd0c87aabdd7507597e7fdead`. Images dieses früheren Stands: API `sha256:02dd0ece4a4af8aef1b4ba402efec0108859f21310f1fa687e86c511eaa0e3a1`, Worker `sha256:e0b8ef01ebff62bbde9f76a1a5b8cc2893e228f6af3b9c16890a8594ea9287a5`. Beide Container meldeten nach der damaligen Bereitstellung `healthy`.

## Erweiterung um Quellsitzungen

Quelle `5b5ece8fcdc85cd8fbde75bcdc81bdeb334eed81`, bereitgestellt unter `/opt/vela/releases/sessions-5b5ece8`: 161 Linux-Tests und 18 Frontendtests bestanden. Geprüft sind Domainzuordnung und Filterung, Importgrenzen, atomare private Speicherung, Status ohne Cookie-Werte sowie die Bereinigung von Arbeitskopien bei Erfolg, Fehler, Abbruch und Wiederanlauf. Alle Tests verwenden synthetische Sitzungsdaten.

Die neue API liefert die fünf Quellsitzungszustände. Ungültiger Import endet mit HTTP 422, fremder Origin mit 403 und unbekannte Quelle mit 404. Browserprüfung: Der Hinweis am Instagram-Fehler führt zur passenden Sitzungseinstellung; Desktop und 390-px-Ansicht wurden geprüft. Ein anonymer Videodownload wurde erneut bis zum UNAS mit passender SHA-256 geprüft; die eigene Testdatei und zugehörigen Aufträge sind bereinigt.

Der untersuchte Instagram-Abruf wurde von der Quelle anonym mit einer Zielgruppenbeschränkung abgelehnt. Ein gültiger Sitzungsexport des Nutzers liegt bei dieser Abnahme noch nicht vor; ein erfolgreicher Download dieses konkreten Videos ist deshalb ausdrücklich nicht bestätigt. Externe Portale werden nicht als Fallback verwendet.

Aktuelle Images: API `sha256:00f59fbe18558019399768e5dd88327459372357cff8793e52ef14c1fcb0ac87`, Worker `sha256:be655906afe87175355725a3213fcfde742b53591eaf808958f69c072e58814f`; beide `healthy`. Die vorangehenden Images und Prüfdaten dokumentieren den früheren Stand.
