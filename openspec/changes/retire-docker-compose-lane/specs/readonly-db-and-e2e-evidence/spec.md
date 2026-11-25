## REMOVED Requirements

### Requirement: Docker evidence location

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes
and nothing builds or starts the image. No Docker build, compose or container smoke run exists any more, and the
review-artifact scenario names the directory of a change archived long ago.

**Migration**: None.
