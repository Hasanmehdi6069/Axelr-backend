# Capability Evidence Details

This file contains detailed evidence of all capabilities used by the axelr-inbox-organizer skill, aligned with the uliya-skill-kit requirements.

## Inventory Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: find
**Purpose**: List all files in the user-authorized inbox folder to establish the complete set of documents that need processing. This is a read-only operation that does not modify any files.

## Stat Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: stat
**Purpose**: Retrieve file metadata including size, modification time, and absolute path. This information is used to build the initial inventory and verify file properties before any operations.

## Hash Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: sha256sum
**Purpose**: Generate a cryptographic hash of each file for deduplication and undo proof. This ensures we can identify duplicate files and maintain an auditable record of all file movements.

## Extract Capability
**Expert**: document-agent
**Capability**: ug-document
**Action**: extract
**Purpose**: Extract key metadata from documents including date, sender, amount, due date, and document type. This metadata is used to automatically categorize and organize files into the correct Year/Month/Category structure.

## Create Folder Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: mkdir
**Purpose**: Create the necessary Year/Month/Category folder structure if it doesn't already exist. This ensures we have a valid location to move processed files into.

## Move Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: cpmv
**Purpose**: Move files from the inbox to their target location with overwrite protection enabled. This prevents accidental file overwrites and maintains data integrity.

## Write Manifest Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: cpmv
**Purpose**: Append a record of each file movement to an undo manifest (undo.jsonl). This creates an auditable log that can be used to revert all changes if needed.

## Read Summary Capability
**Expert**: file-agent
**Capability**: ug-filesystem
**Action**: cat
**Purpose**: Read and render a daily summary of all processed files. This provides the user with a clear overview of what changes were made to their document library.

## Compliance Notes
- No additional capabilities outside of ug-filesystem and ug-document are used
- No custom scripts, network calls, background processes, or credentials are required
- All processing is performed on-device with user approval required before any file modifications