# Media Downloader

Webbgränssnitt för yt-dlp + svtplay-dl.

## Funktioner

- Auto-val av yt-dlp/svtplay-dl
- Kvalitetsval
- Undertexter laddas alltid ner
- Standardmapp: `Nedladdningar Osorterade`
- Välj befintlig mapp under `/downloads`
- Skriv ett nytt mappnamn; mappen skapas automatiskt
- Pågående jobb med procent och status
- Misslyckade jobb visar felstatus och senaste output
- Nedladdade filer visas inklusive undermappar
- YouTube använder yt-dlp; övriga URL:er använder svtplay-dl
- TV4 Play-token kan sparas gemensamt för alla användare

## TrueNAS

Host-mappen `/mnt/Hem-NAS/media/UWTD-Nedladdningar` monteras som `/downloads`.

Port: `30120`.

TV4 Play-token sparas i Docker-volymen `media-downloader-config` och skickas till
`svtplay-dl` vid nedladdning.

## Start

Bygg/deploya med Portainer Stack eller:

```bash
docker compose up -d --build
```
