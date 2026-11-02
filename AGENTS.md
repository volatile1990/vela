# Vela

Vela ist eine eigenständige Anwendung für Markus' Heimserver. Kanonische Quellen liegen in diesem Repository; Infrastrukturzuordnung wird zusätzlich im privaten Repository `volatile1990/network-misc` dokumentiert.

- Vela hat auf ausdrücklichen Nutzerwunsch keine Anmeldung und kein Benutzerkonto. Ausschließlich das Trusted-Netz `192.168.1.0/24` darf den vorgeschalteten Caddy erreichen. Host-/Origin-Prüfung und die getrennten Container-Netze bleiben erforderlich.
- Downloads gehören nach `\\unas.home.arpa\Unprotected\Vela`. Die fehlende Redundanz ist ausdrücklich akzeptiert. Keine zusätzlichen Medienbackups aufgrund dieses Umstands einrichten oder erneut zur Entscheidung stellen.
- SQLite und begrenzte Arbeitsdaten bleiben lokal auf dem Heimserver. API und Worker sind getrennte Dienste; Worker-Neustarts dürfen die API nicht anhalten.
- Vela wird direkt auf dem Heimserver bereitgestellt. Software nur auf ausdrücklichen Nutzerauftrag in Orbit veröffentlichen; ein Build-, Versions- oder Bereitstellungsauftrag allein erlaubt dies nicht. Erst bei einem solchen Orbit-Auftrag `C:/Users/marku/.codex/skills/orbit-release/SKILL.md` anwenden. Diese Entscheidung vom 02.11.2026 ersetzt die frühere automatische Veröffentlichungsvorgabe.
- Für Heimserver-/UNAS-Arbeit den Skill `home-infrastructure` und die vorhandenen strikt geprüften Verbindungen verwenden. Geheimnisse und produktive Datenbanken bleiben außerhalb von Git und Releasepaketen.

Prüfungen: `python -m pytest`, im Frontend `npm test` und `npm run build`; der Docker-Build mit Ziel `test` prüft zusätzlich die Linux-Prozess- und Dateisystemfälle. Reale Engineänderungen benötigen einen passenden Download-Smoke neben den lokalen Tests.
