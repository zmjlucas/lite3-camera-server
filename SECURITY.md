# Security

This project uses SSH to access the perception host. The native RealSense/V4L2 HTTP server binds only to loopback, checks same-origin access when an Origin header is present, and requires a fresh session bearer token for camera frames and health data. Host headers are unrestricted. Do not expose it to the internet through a reverse proxy or port forwarding. ROS mode uses an external `web_video_server`; this repository does not add authentication to that upstream server.

Keep session links and Authorization headers private. The token travels from the server to the launcher over SSH stdout, then to the browser in a URL fragment; it is not put in remote command arguments or an HTTP query. The viewer removes the fragment and uses tab session storage so refreshes work. If browser storage is disabled, reopen the session link after a refresh. Restarting the server invalidates its old token. These controls do not isolate processes already running as your OS account.

Keep SSH host key checks enabled. Do not commit passwords, private keys, tokens, or logs with credentials. Device diagnostics and camera images can contain private information. Before reporting a problem, redact host addresses, usernames, device serial numbers, ROS identifiers, local paths, and session links from logs. Keep real hardware session reports outside the repository. Deployment uploads only the two allowlisted runtime files and rejects symlinked source paths.

Both author and committer metadata are public when commits are published. Deleting a file does not remove previously published information from Git history; history cleanup and GitHub-retained references must be handled separately.

Security fixes target `main` first. To report command injection, authentication bypass, or another security issue, use the repository's GitHub **Security > Report a vulnerability** form. Include the affected version, steps to reproduce the issue, and its effects. Do not publish credentials or sensitive details of an unfixed vulnerability in a public issue.
