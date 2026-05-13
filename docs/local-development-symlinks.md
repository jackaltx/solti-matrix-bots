# Local Development with Symlinks

## The problem

Ansible resolves collection roles and plugins from `~/.ansible/collections/` (or paths in `ansible.cfg`).
When you're actively developing a collection, you don't want to publish to Galaxy and reinstall on
every change. A symlink makes Ansible treat your working directory as the installed collection.

## How `create_symlinks.sh` works

```bash
./create_symlinks.sh
```

This creates:

```
~/.ansible/collections/ansible_collections/jackaltx/solti_matrix_bots  →  /path/to/solti-matrix-bots
```

Ansible now finds `jackaltx.solti_matrix_bots` roles and plugins directly from your checkout.
Edit a file, run a playbook — no reinstall needed.

## Using the collection in another project (e.g. mylab)

After running `create_symlinks.sh` from the `solti-matrix-bots` directory, any project that
references `jackaltx.solti_matrix_bots` will automatically use your local version:

```yaml
# mylab playbook or role
- hosts: matrix_bots
  roles:
    - jackaltx.solti_matrix_bots.matrix_watcher
```

Or in `requirements.yml` for documentation purposes — but since the symlink is already in
`~/.ansible/collections/`, Ansible will use it without installing from Galaxy.

## Workflow for mylab development

```bash
# 1. Set up the symlink (one-time per machine)
cd ~/sandbox/ansible/jackaltx/solti-matrix-bots
./create_symlinks.sh

# 2. Verify Ansible can find it
ansible-galaxy collection list | grep solti_matrix_bots

# 3. Develop in solti-matrix-bots — changes are live immediately
# No reinstall, no publish cycle

# 4. Run from mylab using the collection roles normally
cd ~/sandbox/ansible/jackaltx/mylab
./manage-bot.sh matrix-watcher deploy
```

## Switching back to the Galaxy-installed version

```bash
# Remove the symlink
rm ~/.ansible/collections/ansible_collections/jackaltx/solti_matrix_bots

# Install from Galaxy
ansible-galaxy collection install jackaltx.solti_matrix_bots
```

## Caveats

- **Molecule testing:** Some Molecule scenarios build the collection tarball and install it into
  an isolated environment. The symlink is ignored in that context — remove it before running
  Molecule if you hit collection-not-found errors, or set `ANSIBLE_COLLECTIONS_PATH` in
  `molecule.yml` to point at your checkout directly.

- **`-f` flag in the script:** `create_symlinks.sh` uses `ln -sf` so re-running it is safe —
  it replaces an existing symlink rather than erroring.

- **Multiple machines:** The symlink is local to the machine. On a remote host (e.g. when using
  `manage-bot.sh -h myserver`), Ansible runs locally and SSHes tasks over — the symlink on
  your dev machine is sufficient.

- **Collection name must match:** The symlink target directory name must be the collection's
  `name` from `galaxy.yml` — `solti_matrix_bots` (underscores). The repo directory name
  (`solti-matrix-bots` with hyphens) doesn't matter; only the symlink name does.
