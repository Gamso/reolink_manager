## Developing with Visual Studio Code + devcontainer

The easiest way to get started with custom integration development is to use Visual Studio Code with devcontainers. This approach will create a preconfigured development environment with all the tools you need.

In the container you will have a dedicated Home Assistant core instance running with your custom component code. You can configure this instance by updating the `./devcontainer/configuration.yaml` file.

**Prerequisites**

- [git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- Docker
  -  For Linux, macOS, or Windows 10 Pro/Enterprise/Education use the [current release version of Docker](https://docs.docker.com/install/)
  -   Windows 10 Home requires [WSL 2](https://docs.microsoft.com/windows/wsl/wsl2-install) and the current Edge version of Docker Desktop (see instructions [here](https://docs.docker.com/docker-for-windows/wsl-tech-preview/)). This can also be used for Windows Pro/Enterprise/Education.
- [Visual Studio code](https://code.visualstudio.com/)
- [Remote - Containers (VSC Extension)][extension-link]

[More info about requirements and devcontainer in general](https://code.visualstudio.com/docs/remote/containers#_getting-started)

[extension-link]: https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers

**Getting started:**

1. Clone the repository to your computer.
2. Open the repository using Visual Studio code.

When you open this repository with Visual Studio code you are asked to "Reopen in Container", this will start the build of the container.

_If you don't see this notification, open the command palette and select `Remote-Containers: Reopen Folder in Container`._

### Tasks

The devcontainer comes with some useful tasks to help you with development, you can start these tasks by opening the command palette and select `Tasks: Run Task` then select the task you want to run.

Task | Description
-- | --
Run Home Assistant on port 8123 | Launch Home Assistant with your custom component code and the configuration defined in `.devcontainer/configuration.yaml`.
Restart Home Assistant on port 8123 | Kill and relaunch the running instance.
Start coverage | Run the pytest suite under coverage and produce an HTML report.

### Manual end-to-end testing

Reolink Manager has no simulated-camera fixture (unlike a template climate entity, there's no lightweight stand-in for a real Reolink camera/NVR). To actually exercise it in the devcontainer:

1. Add the official **Reolink** integration first, pointed at a real camera or NVR channel on your network.
2. Add **Reolink Manager** and pick that Reolink entry.
3. Check the switches it creates under the camera's device page.

### Step by Step debugging

With the development container, you can test your custom component in Home Assistant with step by step debugging.

The `.devcontainer/configuration.yaml` file already has `debugpy:` enabled. Launch the task `Run Home Assistant on port 8123`, and launch the debugger with the existing debugging configuration `Home Assistant (debug)`.

For more information, look at [the Remote Python Debugger integration documentation](https://www.home-assistant.io/integrations/debugpy/).
