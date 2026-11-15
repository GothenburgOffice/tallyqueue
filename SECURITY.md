# Security

TallyQueue is a permanent beta (0.x) that its owner maintains in spare time. Only the latest
version on `main` gets fixes, and there is no promised response time.

## Reporting a vulnerability

Please don't open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/GothenburgOffice/tallyqueue/security/advisories/new)
for this repository. Never include a real receipt, real figures or a real access token in a
report: describe the problem with made-up data.

## What the server is built to protect

- **Only approved users get in.** The operator lists them in `TALLYQUEUE_USERS`. The server
  keeps the SHA-256 of each access token, never the token, and with nobody listed nobody gets
  in. The same token opens the MCP endpoint and the web page's sign-in.
- **Users never see each other's data.** Each user has their own folder, and every tool and
  page works from the user that the request's own token or session proves.
- **Receipts can't run as pages.** A receipt is served to its owner only, by the type its bytes
  have. Anything that is not a PDF or an image is sent as plain text in a sandbox. The pages
  themselves use no JavaScript and their content policy forbids it.
- **Forms can't be posted from other sites.** Every form post is checked for its origin, the
  session and a CSRF token; the session cookie is HttpOnly and SameSite.
- **Tokens can't be guessed.** A token is 256 random bits, and the server refuses anything
  shorter than the ones it makes. There is no sign-in throttle, on purpose: it would add
  nothing against tokens of that size, and it would let a stranger lock the owner out.

## What it leaves to the operator

- **Rate limits**, if you want them, in the reverse proxy.
- **HTTPS.** The server speaks plain HTTP on a port that only the machine itself can reach. Put
  an HTTPS reverse proxy such as Caddy in front (README, "Behind Caddy").
- **Encryption at rest and backups.** Items and receipts are ordinary files in the data volume.
- **Keeping tokens safe.** A token is as good as the user's sign-in. To revoke one, replace its
  pair in `TALLYQUEUE_USERS` and restart; that also ends the user's web sessions.

## Known limits

- An AI client that holds a user's token can read and change that user's whole list. It can't
  delete items; only the web page can.
- Web sessions are kept in memory, so a restart signs everyone out.
- The server is built for one process. Don't run several copies against one data volume.
