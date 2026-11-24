# Security

The native RealSense/V4L2 server assumes the entire local network is trusted. It listens on all IPv4 interfaces by default and has no HTTP authentication: reachable clients can view frames, inspect camera status and select the shared source. Requests with an Origin header must match the current origin; this browser check is not access control. Host headers are unrestricted. Use `--bind 127.0.0.1` when starting the server manually if only SSH tunnel access is wanted. Internet exposure is outside this trust model.

SSH is used for deployment, remote startup and optional tunnels, with its existing authentication and host key verification. A native server started by `view` persists independently of clients until explicitly stopped. ROS mode uses an external `web_video_server`; this repository does not add authentication to that upstream server.

Keep SSH host key checks enabled. Do not commit passwords, private keys, tokens, or logs with credentials. Device diagnostics and camera images can contain private information. Before reporting a problem, redact host addresses, usernames, device serial numbers, ROS identifiers, local paths from logs. Keep real hardware session reports outside the repository. Deployment uploads only the two allowlisted runtime files and rejects symlinked source paths. Windows deployment also rejects reparse points (including directory junctions) and holds source path handles without write/delete sharing until capture finishes.

Both author and committer metadata are public when commits are published. Deleting a file does not remove previously published information from Git history; history cleanup and GitHub-retained references must be handled separately.

Security fixes target `main` first. To report command injection, unintended file access, or another security issue, use the repository's GitHub **Security > Report a vulnerability** form. Include the affected version, steps to reproduce the issue, and its effects. Do not publish credentials or sensitive details of an unfixed vulnerability in a public issue.
