# Contributing

Contributions are welcome.

## Before opening a pull request

- Base changes on the current `main` branch.
- Keep changes focused and preserve the existing project structure/naming.
- Do not commit `secrets.ini`, local board registries, local settings, generated
  results, virtual environments, or generated firmware configuration.
- Run the relevant host checks and PlatformIO build(s).
- If a change affects hardware/RF behavior, describe which DUT/reference ESP32
  profile, fixture setup, and host OS were tested physically.

## CI

Pull requests are expected to pass the GitHub Actions workflow, including:

- Python/configuration validation
- Linux and Windows host smoke checks
- all supported DUT PlatformIO firmware profiles
- all supported reference-firmware PlatformIO profiles

CI verifies build and host-tool behavior but does not replace physical board
testing.

## Reports and logs

Generated reports can contain MAC addresses, local IP addresses, BSSIDs, SSIDs,
and serial/network diagnostics. Review and redact attachments before posting
them publicly.

## Bugs

Use the GitHub bug-report template and include only the information needed to
reproduce the problem.
