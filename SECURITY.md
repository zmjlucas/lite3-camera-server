# Security

This project uses SSH to access the perception host. The camera HTTP server has no separate authentication and binds only to the loopback address. Do not expose it to the internet through a reverse proxy or port forwarding.

Keep SSH host key checks enabled. Do not commit passwords, private keys, tokens, or logs with credentials. Device diagnostics and camera images can contain private information. Before you report a problem, remove unrelated private information.

Security fixes target `main` first. To report command injection, authentication bypass, or another security issue, use the repository's GitHub **Security > Report a vulnerability** form. Include the affected version, steps to reproduce the issue, and its effects. Do not publish credentials or sensitive details of an unfixed vulnerability in a public issue.
