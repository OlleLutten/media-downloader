# Media Downloader

En enkel Docker-baserad webbserver för yt-dlp och svtplay-dl.

## Portainer

Stacken bygger imagen lokalt från Dockerfile.

1. Lägg hela mappen i ett GitHub-repo.
2. Portainer -> Stacks -> Add stack -> Repository.
3. Välj repository och branch.
4. Deploy the stack.
5. Öppna `http://TRUENAS-IP:30120`.

Standardmapp:
`/mnt/Hem-NAS/media/UWTD-Nedladdningar`

## Viktigt

Det här är en första version. Den saknar autentisering, jobbkön och mer avancerad felhantering. Kör den därför helst endast på LAN/Tailscale tills autentisering lagts till.

Använd bara tjänsten för innehåll du har rätt att ladda ner och i enlighet med respektive tjänsts villkor.
