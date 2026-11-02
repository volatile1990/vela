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

Images dieser Erweiterung: API `sha256:00f59fbe18558019399768e5dd88327459372357cff8793e52ef14c1fcb0ac87`, Worker `sha256:be655906afe87175355725a3213fcfde742b53591eaf808958f69c072e58814f`; beide bei dieser Abnahme `healthy`. Die vorangehenden Images und Prüfdaten dokumentieren den früheren Stand.

## Instagram-Anmeldung mit eigenem Browserprofil

Die Buildprüfung des neuen Stands besteht aus **222 bestandenen Linux-Tests** und **25 bestandenen Frontendtests** einschließlich Production-Build. Synthetische Prüfungen decken verschlüsselte Zugangsspeicherung, Dateirechte, Kontowechsel, Wiederanlauf ohne wiederholte Passwortversuche, Challenge → Ready, automatische Fortsetzung und die Begrenzung der Erneuerung pro Nutzer-Versuch ab. Drei reproduzierte Nebenläufigkeitsfehler beim Entfernen beziehungsweise Öffnen wurden korrigiert und durch Regressionstests abgesichert.

Ein echter Chromium 156.0.8078.73 erreichte die Instagram-Anmeldeseite mit HTTP 200 unter den vorgesehenen Containergrenzen, ohne Anmeldedaten zu verwenden oder einen Login abzusenden. Der echte Vela-Controller lieferte anschließend ein 92.214 Byte großes PNG und nahm eine begrenzte Tastenaktion an. Ein synthetisches Passwortfeld mit sichtbarem Text wurde bei zwei unterschiedlichen Werten in identische maskierte PNGs umgewandelt. Die aktuelle Formularvariante mit `email`/`pass`, Submit-Input und Cookie-Dialog wird berücksichtigt.

Eine reale Netzwerkprobe vom vorgesehenen Browserplatz im Workernetz bestätigte öffentlichen HTTPS-Zugriff und blockierte Verbindungen zu Heimserver, NAS, Bridge-Gateway, API und IPv6. Ein echter Instagram-Login und der Download des ursprünglichen Nutzervideos sind bis zur Bereitstellung eines berechtigten Nutzerkontos offen; die synthetischen Prüfungen ersetzen diese Abnahme nicht.

Bereitgestellt am 2. November 2026 unter `/opt/vela/releases/instagram-f4ecd18`, Quelle `f4ecd186b935c2d5c0ad3b5f409d6dc3eede7eaf`. API `sha256:63aa2c8c674a170b91cb1ceda4d094b2f768c535c04813b2d76e48f5123ba96c`, Worker `sha256:c1dc284ff46513455a6035848a2fe2241bfa13d13d3347f16b8d5edc1f4a431b`, Browser `sha256:30d23a82a019171b831d5f0c4e17745f2a4ddd711f5ca7d9e927cbeabe9d940d`: alle drei `healthy`. Die neuen privaten Verzeichnisse haben Modus `0700` und Eigentümer `10020:10020`. Die API erreicht den Browser über den nur dort eingebundenen Unixsocket. Ungültige Zugangseingaben ergeben 422, ein fremder Origin 403, ohne Daten zu speichern. Zum Prüfzeitpunkt ist kein Konto hinterlegt.

Ein erneuter anonymer MDN-Videodownload nach der Bereitstellung wurde über die API und direkt auf dem UNAS unabhängig geprüft: 1.128.375 Byte, SHA-256 `0cd83d944a6ca7822b4a8306cecc60a36e859b041f6702c6a1ad9ead78924451`, ohne Quellsitzung. Die eigene Testdatei und exakt zwei eigene Testaufträge wurden anschließend entfernt; Nutzeraufträge blieben erhalten. Ein Neustart ausschließlich des Browserdienstes ließ zwölf aufeinanderfolgende API-Prüfungen mit HTTP 200 erfolgreich. Desktop- und 390-px-Ansicht der neuen Zugangseingabe wurden im Browser geprüft; kein horizontaler Überlauf. Caddy, NAS-Zuordnung und andere Anwendungen wurden nicht geändert. Keine Orbit-Veröffentlichung.

## Browserprofil nach hartem Containerabbruch

Ein zusätzlicher isolierter Crash-Test ohne Zugangsdaten zeigte einen konkreten Wiederanlauffehler: Chromium hinterließ drei Singleton-Sperrlinks und verweigerte das Profil in einem neuen Container mit anderem Hostnamen. Der Browserdienst bereinigt jetzt ausschließlich diese festen Linknamen nach exklusivem Profilbesitz und Prüfung auf aktive Prozesse. Vier zusätzliche Linux-Regressionstests prüfen verwaiste und aktive Sperren, unerwartete Dateien sowie den exakten Prozessabgleich. **226 Linux-Tests bestanden**, die 25 Frontendtests bleiben unverändert gültig.

Der echte Controller wurde danach in einem wegwerfbaren Container mit einem separaten Testprofil hart beendet. Ein neuer Container konnte das Profil wieder öffnen; eine zusätzliche Profildatei blieb erhalten. Symlinkziele wurden nicht entfernt. Die Testcontainer und ihr eigenes Profil wurden anschließend bereinigt. Das ist ein belegter Container-Crash/Reopen, kein vollständiger Host-Reboot und kein Test mit einem echten Instagram-Konto.

Der Browser-Fix aus `71b9c2f1e9e333b9c80996bd31fa3ccc558e7699` ist unter `/opt/vela/releases/instagram-71b9c2f` bereitgestellt. Browserimage `sha256:07856c088b2db883997b2d0cd632243d9e79d9954d6ea0d9222afb77319bbfb3`, Laufzeit-Codehash `c0d9f83280ab87b1c3cd1556af2c36b76700bd8f1287d94fd421ae708f845669`. API und Worker behalten die oben protokollierten Images aus `f4ecd18` und liefen während dieser gezielten Browseraktualisierung weiter: nochmals zwölf API-Prüfungen, alle HTTP 200. Anschließend alle drei Container `healthy`. Der Instagram-Zugang bleibt bei dieser abschließenden Prüfung noch unkonfiguriert.
