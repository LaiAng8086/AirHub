# Security

AirHub treats API keys, login credentials, downloaded documents, transcripts,
logs, caches, and generated model files as local runtime data. They are excluded
from version control by default.

- Copy `config/deepseek.example.json` to `config/deepseek.json` and keep the
  resulting file private (`chmod 600 config/deepseek.json`).
- Xiaoyuzhou credentials are written to
  `config/xiaoyuzhou_credentials.json`; never commit that file.
- Review staged changes with `git status` and a secret scanner before publishing.
- Do not use AirHub to fetch content you are not permitted to access or archive.

Please report a suspected vulnerability privately to the repository maintainer
instead of opening an issue containing credentials or personal data.
