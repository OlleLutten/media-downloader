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
- YouTube använder yt-dlp; övriga URL:er använder svtplay-dl.
- Kapitel och thumbnail laddas alltid ner automatiskt.
- TV4 Play-token kan sparas gemensamt för alla användare
- Filer och mappar kan laddas upp till en valbar undermapp

## TrueNAS

Host-mappen `/mnt/Hem-NAS/media/UWTD-Nedladdningar` monteras som `/downloads`.

Port: `30120`.

TV4 Play-token sparas i Docker-volymen `media-downloader-config` och skickas till
`svtplay-dl` vid nedladdning.

Uppladdningar sparas i `/mnt/Hem-NAS/media/uppladdningar` på TrueNAS.

## Start

Bygg/deploya med Portainer Stack eller:

```bash
docker compose up -d --build
```


### v17-fixed
- Utgår från v17.
- Fixar SVT Play `--all-episodes` så att URL:en skickas med i kommandot.
- Behåller svtplay-dl:s automatiska filnamn, inklusive punkt-/ID-strukturen, så att video och undertext får exakt matchande namn.
- Låser svtplay-dl till 4.197.
