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

## TrueNAS

Host-mappen `/mnt/Hem-NAS/media/UWTD-Nedladdningar` monteras som `/downloads`.

Port: `30120`.

## Start

Bygg/deploya med Portainer Stack eller:

```bash
docker compose up -d --build
```
