# Development Status

## OPS-UDA-001 — UDA application-prefix routing
Status: IN PROGRESS

The live UDA-listed Flask Chat code now accepts one trusted UDA/Caddy proxy forwarding hop, keeps direct LAN root routes, and resolves browser chat polling/submission/clear/shutdown calls within the mounted `/apps/flask-chat/` path. The backend must be ingress-isolated against spoofed forwarded headers. UDA public access remains disabled.

- [ ] UDA compatibility CI passed and merged to main
- [ ] Confirm UDA app listener exists on Ubuntu (prior server audit showed none)
- [ ] User validates messages, agent, attachments and shutdown through UDA
