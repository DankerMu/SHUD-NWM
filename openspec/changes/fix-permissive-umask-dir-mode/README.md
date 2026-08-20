# fix-permissive-umask-dir-mode

safe_fs directory creation pins an explicit mode so provider_atomic's lock-parent gate stops depending on ambient umask
