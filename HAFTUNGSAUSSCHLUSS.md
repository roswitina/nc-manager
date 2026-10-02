# Gewährleistungs- und Haftungsausschluss

**Nextcloud Server Manager** · Urheber: roswitina@hotmail.com · Lizenz: MIT (siehe `LICENSE`)

Dieser Text ergänzt die MIT-Lizenz und erläutert sie auf Deutsch. Rechtlich maßgeblich ist der englische
Lizenztext in `LICENSE`; bei Abweichungen gilt dieser. Mit der Installation oder Nutzung der Software
erklärst du dich mit den folgenden Bedingungen einverstanden.

## 1. Bereitstellung ohne Gewährleistung

Die Software wird unentgeltlich und **„wie besehen“ (as is)** zur Verfügung gestellt, ohne jede ausdrückliche
oder stillschweigende Gewährleistung oder Garantie. Das umfasst insbesondere – ohne Anspruch auf Vollständigkeit –
keine Gewähr für:

- Fehlerfreiheit, Vollständigkeit, Richtigkeit oder Aktualität der Software, ihrer Ausgaben und der Dokumentation,
- Eignung für einen bestimmten Zweck oder Marktgängigkeit,
- ununterbrochenen, störungsfreien oder sicheren Betrieb,
- Kompatibilität mit bestimmten Betriebssystemen, Nextcloud-, PHP-, Datenbank- oder sonstigen Versionen,
- Freiheit von Sicherheitslücken, Schadcode oder Rechten Dritter,
- die Erreichung eines bestimmten Ergebnisses, etwa einer erfolgreichen Aktualisierung oder Wiederherstellung.

## 2. Haftungsausschluss

Soweit gesetzlich zulässig, haften Urheber und Mitwirkende nicht für Schäden jeglicher Art, die aus der
Installation, der Nutzung, der Nichtnutzbarkeit oder in sonstigem Zusammenhang mit der Software entstehen –
gleich aus welchem Rechtsgrund (Vertrag, Delikt, Gefährdungshaftung oder sonstiges). Dazu gehören insbesondere:

- Verlust, Beschädigung, Veränderung oder unbefugte Offenlegung von Daten (auch von Benutzerdaten, Datenbanken,
  Konfigurationen und Backups),
- Ausfall- und Stillstandszeiten von Nextcloud oder anderen Diensten,
- Schäden an Hardware, Betriebssystem, Software oder Konfiguration,
- Sicherheitsvorfälle, auch infolge der Weboberfläche oder ihrer Konfiguration,
- Kosten für Datenrettung, Wiederherstellung, Neuinstallation oder Fehlersuche,
- entgangenen Gewinn, Betriebsunterbrechung, mittelbare Schäden und Folgeschäden,
- Ansprüche Dritter.

## 3. Besondere Risiken dieser Software

Die Software führt über einen privilegierten Hilfsbefehl Aktionen **mit Root-Rechten** aus, die das System
tiefgreifend verändern oder Daten unwiderruflich löschen können. Dazu gehören unter anderem:

- Nextcloud- und App-Updates, Datenbank-Reparaturen und -Konvertierungen,
- die **Wiederherstellung von Backups**, bei der die Datenbank vollständig ersetzt sowie Programmcode und
  gegebenenfalls Benutzerdaten ausgetauscht werden,
- das Löschen von Backups, Papierkorb-Inhalten und Dateiversionen,
- Änderungen an PHP- und PHP-FPM-Konfiguration und das Neuladen von Diensten,
- das Ein- und Ausschalten des Wartungsmodus.

Die Nutzung erfolgt **ausschließlich auf eigene Verantwortung und eigenes Risiko**. Vor jeder ändernden
Aktion ist ein aktuelles, unabhängiges Backup bzw. ein Snapshot anzulegen und dessen Wiederherstellbarkeit
zu prüfen. Neue Versionen sollten zuerst auf einer Testinstanz erprobt werden.

## 4. Backups und Wiederherstellung

Für Vollständigkeit, Konsistenz, Lesbarkeit und Wiederherstellbarkeit der vom Programm erstellten Backups wird
keine Gewähr übernommen – auch dann nicht, wenn die eingebaute Prüfung ein Backup als „in Ordnung“ meldet.
Manager-Backups liegen auf demselben Server und ersetzen keine unabhängige, externe Datensicherung.

## 5. Empfehlungen und Anzeigen

Empfehlungen (etwa für PHP- oder PHP-FPM-Werte), Bewertungen, Diagnosen und Statusanzeigen sind unverbindliche
Hinweise. Sie ersetzen keine fachkundige Prüfung im Einzelfall. Für Entscheidungen auf ihrer Grundlage ist
allein der Betreiber verantwortlich.

## 6. Verantwortung des Betreibers

Der Betreiber ist allein verantwortlich für die Auswahl, Installation, Konfiguration und Absicherung der
Software und des Systems, insbesondere für den Schutz des Zugangs zur Weboberfläche, sichere Passwörter, eine
verschlüsselte Verbindung, regelmäßige Datensicherungen sowie die Einhaltung rechtlicher Vorgaben (etwa
Datenschutz/DSGVO).

## 7. Kein Anspruch auf Support oder Weiterentwicklung

Es besteht kein Anspruch auf Support, Beratung, Fehlerbehebung, Sicherheitsupdates, neue Versionen oder die
Beantwortung von Anfragen. Die Software kann jederzeit ohne Ankündigung geändert oder nicht mehr
weiterentwickelt werden.

## 8. Software Dritter und Marken

Die Software steuert und nutzt Programme Dritter (u.a. Nextcloud, PHP, MariaDB/MySQL, PostgreSQL, Redis, Flask,
Werkzeug, Gunicorn). Für diese gelten ausschließlich deren eigene Lizenzen und Bedingungen; für ihre Funktion
und Fehler wird keine Verantwortung übernommen. Abhängigkeiten werden bei der Installation aus öffentlichen
Paketquellen bezogen.

Nextcloud ist eine Marke der Nextcloud GmbH. Dieses Projekt ist ein unabhängiges Werkzeug, kein offizielles
Produkt der Nextcloud GmbH und steht in keiner Verbindung zu ihr. Alle weiteren genannten Marken gehören ihren
jeweiligen Inhabern.

## 9. Zwingendes Recht

Die vorstehenden Ausschlüsse gelten nur, soweit dies gesetzlich zulässig ist. Unberührt bleibt eine nach
zwingendem Recht bestehende Haftung, insbesondere für Vorsatz und grobe Fahrlässigkeit, für Schäden an Leben,
Körper oder Gesundheit sowie nach dem Produkthaftungsgesetz. Sollte eine Bestimmung unwirksam sein, bleiben
die übrigen Bestimmungen wirksam.

---

## Disclaimer (English summary)

This software is provided free of charge and **"as is"**, without warranty of any kind, express or implied,
as stated in the MIT License (`LICENSE`), which is the legally binding text. It performs actions with **root
privileges** (updates, database restore, deletion of backups, trash and versions, PHP configuration) that can
irreversibly alter systems or destroy data. Use it entirely at your own risk; always keep a current,
independent backup or snapshot and test on a non-production instance first. To the extent permitted by law,
the author and contributors are not liable for any damages, including data loss, downtime or consequential
damages. No support or updates are owed. Nextcloud is a trademark of Nextcloud GmbH; this project is not
affiliated with or endorsed by Nextcloud GmbH. Liability that cannot be excluded under mandatory law remains
unaffected.
