# Vela

Vela ist eine eigenständige Anwendung für Markus' Heimserver. Kanonische Quellen liegen in diesem Repository; Infrastrukturzuordnung wird zusätzlich im privaten Repository `volatile1990/network-misc` dokumentiert.

- Vela hat auf ausdrücklichen Nutzerwunsch keine Anmeldung und kein Benutzerkonto. Ausschließlich das Trusted-Netz `192.168.1.0/24` darf den vorgeschalteten Caddy erreichen. Host-/Origin-Prüfung und die getrennten Container-Netze bleiben erforderlich.
- Downloads gehören nach `\\unas.home.arpa\Unprotected\Vela`. Die fehlende Redundanz ist ausdrücklich akzeptiert. Keine zusätzlichen Medienbackups aufgrund dieses Umstands einrichten oder erneut zur Entscheidung stellen.
- SQLite und begrenzte Arbeitsdaten bleiben lokal auf dem Heimserver. API und Worker sind getrennte Dienste; Worker-Neustarts dürfen die API nicht anhalten.
- Auslieferbare Versionen werden nach `C:/Users/marku/.codex/skills/orbit-release/SKILL.md` über das lokale Orbit bereitgestellt und dort geprüft. Gewöhnliche Codeänderungen ohne Versionsauftrag lösen keinen Release aus.
- Für Heimserver-/UNAS-Arbeit den Skill `home-infrastructure` und die vorhandenen strikt geprüften Verbindungen verwenden. Geheimnisse und produktive Datenbanken bleiben außerhalb von Git und Releasepaketen.

Prüfungen: `python -m pytest`, im Frontend `npm test` und `npm run build`; der Docker-Build mit Ziel `test` prüft zusätzlich die Linux-Prozess- und Dateisystemfälle. Reale Engineänderungen benötigen einen passenden Download-Smoke neben den lokalen Tests.
