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
