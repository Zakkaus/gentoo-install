# README writing standard

This standard applies to the five README files and `CONTRIBUTING.md`. Repository instructions take precedence when they impose a stricter rule.

## Audience and purpose

The README serves an operator evaluating or starting the installer. It identifies the project, states the verified scope, lists requirements, warns before destructive actions, gives the shortest working path and links detailed contributor material.

`CONTRIBUTING.md` serves a developer changing the project. It owns architecture, test, VM and commit instructions. The README set links to it without duplicating those instructions.

## Information order

Use this order when the section applies:

1. project identity and purpose;
2. capabilities;
3. verification status and known limits;
4. requirements;
5. destructive-operation warning;
6. shortest installation path;
7. resume constraints;
8. configuration-file reference;
9. binary-package behavior;
10. exit codes;
11. contribution link.

Put a prerequisite or warning before the command that depends on it. Put an observable result next to its command.

## Claims

State a feature as verified only when current evidence covers the named combination. Distinguish these categories:

- implemented and verified end to end;
- implemented and covered by unit or plan tests only;
- implemented but currently failing or awaiting a rerun;
- planned but not implemented.

The README may list the first category as a capability. The second and third categories need an explicit verification qualification. The fourth category belongs in project planning documents, not the README.

Avoid promotional adjectives, unsupported absolutes and broad compatibility claims. Replace `works on any live system` with the tested architecture and recognized distribution families. Replace `working examples` with `schema references` unless each example has a current successful end-to-end record.

## Sentences and terms

Use complete declarative sentences. A compact list after a bold subject is acceptable only when the sentence still has a predicate. Keep one fact or one cause-and-effect relationship per sentence.

Name the exact object: target system, disk selector, partition, journal, operation plan, binhost, package, configuration hash or installer revision. Do not use vague substitutes such as machine, path, host, layout or state when more than one referent is possible.

Keep established Gentoo and Linux terms in English when translation would blur the distinction. In particular, `binhost` is not a generic host, `overlay` is not the main repository, `initramfs` is not the installed root, and a QR code represents the uploaded page address rather than storing a second copy of the configuration.

## Locale rules

English is the factual source. It uses third person and no direct reader address.

Traditional Chinese uses the `zh-TW` script consistently. It retains the project's selected terms: `原始碼`, `鏡像`, `參數`, `卸載`, `二進位套件`, `儲存庫` and `顯示管理器`. Prose uses Chinese names for the five languages rather than inserting other scripts.

Simplified Chinese uses the `zh-CN` script consistently. Preferred terms include `源代码`, `二进制软件包`, `仓库`, `显示管理器`, `卸载` and `配置文件`. Prose uses Chinese names for the five languages rather than inserting other scripts.

Japanese uses explicit predicates such as `対応する`, `必要である`, `記録する` and `意味する`. Avoid noun fragments, reciprocal wording where references are one-way, and literal constructions such as `質問を取り除く`.

Korean uses complete predicates and correct particles without spaces before them. Prefer `표시하다` for console rendering, `소스에서 빌드하다` for source builds and `대화형 터미널` for an interactive terminal. Avoid colloquial verbs such as `건드리다` and ambiguous nouns such as `기기` when the target system is meant.

## Examples, links and images

Keep shell commands identical across locales except for explanatory comments and locale values. Preserve option order when changing it would make cross-language comparison harder.

Do not publish an incomplete TOML fragment as a working configuration. Prefer a validated fixture link plus a warning about test selectors and credentials. If an embedded example is necessary, extract it in a test and run the real parser and validator against it.

Use descriptive link text. Every relative target must exist from the README's final location. Image alternative text states what the image shows without repeating the surrounding paragraph.

## Review checklist

- The opening names the project, artifact type and purpose.
- The verified and unverified scopes are separate.
- Safety text precedes every real unattended command.
- The common path is complete and uses commands that exist.
- Resume text states session, revision, configuration and journal-lifetime limits.
- Configuration examples parse and validate, or are clearly labeled schema references.
- QR wording says that the code represents the uploaded page address.
- Technical terms preserve their Gentoo or Linux meaning.
- Every locale has the same facts, commands, links, warnings and section order.
- Chinese lint passes at warning level, and Japanese and Korean have language-specific review.
- `CONTRIBUTING.md` contains contributor instructions; README files only link to it.
