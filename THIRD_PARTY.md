# Third-party software

- [Grocy](https://github.com/grocy/grocy), copyright Bernd Bestel and contributors, MIT License. Grocy is run as a separate service and is the inventory source of truth. This repository is an independent companion interface, not an official Grocy project.
- [LinuxServer.io Grocy image](https://github.com/linuxserver/docker-grocy), upstream container distribution. Refer to its repository for image and dependency notices.
- Python runtime dependencies retain their respective licenses; see `pyproject.toml` and `uv.lock`.
- The Gemini service is optional and separate from the MIT-licensed application. API use is governed by the provider's terms and may incur fees. Household context sent to the model includes inventory data needed for the request; API credentials remain on the backend.

No Grocy source code or third-party images are copied into the web assets.
