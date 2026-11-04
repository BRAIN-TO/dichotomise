# Why dichotomise exists

**TL;DR:** Repeated scanner filenames can hide misfiled and duplicated DICOM instances. These incidents show what was found, not why the exports occurred.

## The default export

**TL;DR:** XA60+ filenames repeat across series; the SOP Instance UID in each header identifies the instance.

For a given study, the XA60+ export places DICOMs from each series into sub-folders named:
```text
<ProtocolName>_<SeriesNumber>_MR/
```

The DICOMs inside are named `1.dcm`, `2.dcm`, and so on. The numbering starts again in 
every series folder and recurs across subject exports. 

```text
subject-export/
  DWI_21_MR/
    1.dcm
    2.dcm
  fMRI_22_MR/
    1.dcm
    2.dcm
```
`1.dcm` in one series may be entirely unrelated to `1.dcm` in another. The
files can contain different acquisitions and have different file sizes.

## Why repeated filenames are positively dreadful

**TL;DR:** A filename is only meaningful while its series folder stays with it.

Naming files `1.dcm`, `2.dcm`, `3.dcm` is the worst possible choice. It is a
regression from something that previously worked well.

| Export | File naming |
|---|---|
| Older exports | Distinct `.IMA` filenames |
| XA30 exports | Distinct `.dcm` filenames |
| XA60+ exports | `1.dcm`, `2.dcm`, … repeated in every series folder |

Every DICOM carries a globally unique SOP Instance UID. The XA60 export retains 
that identifier in the header but does not use it to distinguish the exported
filename. The file's name identifies it only while its surrounding folder
structure remains intact. That is a data-integrity problem at the point of
export, before a researcher runs a pipeline or moves a file.

FAIR principle **F1** calls for globally unique, persistent identifiers for
data. The DICOM header supplies one; the exported filename hides it from
ordinary file operations. BIDS takes the opposite approach to filenames: its
applicable entities identify the data within the filename itself.

### Why this causes problems

**TL;DR:** Repeated names make identity, placement, collisions and provenance harder to check.

1. **Repeated filenames hide identity.** XA60+ assigns the same name to
   unrelated DICOMs. A file separated from its folder cannot be identified by
   name, although its SOP Instance UID remains in its header. This undermines
   findability at the file-system level and makes safe handling depend on
   reading DICOM metadata every time. Dichotomise uses the SOP Instance UID
   from the header in the rectified filename.

2. **A folder can disagree with its contents.** The export uses a series
   folder to provide context that the filename lacks. If that folder
   contradicts the DICOM header, the name offers no independent clue.
   
   [Example Incident 1](#incident-1-dicom-placed-in-the-wrong-series) shows that this
   disagreement occurred within a single export. Dichotomise compares each
   file's series details with its folder's majority and sends disagreements
   to review.

3. **Repeated names can collide.** When exports are
   combined, flattened or restored into one directory, repeated names
   collide. Depending on the operation and its settings, a collision can
   overwrite a file or stop the transfer. Either way, the filename cannot
   distinguish the DICOMs involved. Safe reuse demands an explicit identity
   check, not trust in the exported names. Dichotomise checks file bytes for
   duplicates within each source folder and separates later copies for review.

4. **Filenames alone do not preserve provenance.** Matching
   filenames do not establish that two DICOMs contain the same instance or
   the same scan content. Counters alone cannot verify completeness, detect a
   misplaced instance or establish where a file came from. FAIR **R1.2**
   calls for detailed provenance; losing a file's folder context makes that
   provenance harder to recover from the export, even though metadata remains
   in the DICOM.

   [Example Incident 2](#incident-2-duplicated-dwi-series) shows
   why a plausible filename sequence cannot serve as an audit. Dichotomise
   archives the source DICOMs and records manifests before processing, so the
   original files and their paths remain available for review.

## Example incidents

**TL;DR:** We found files in the wrong series folder and byte-identical repeated exports.

We noticed inconsistencies in scanner exports and inspected the DICOM metadata
manually. Both incidents below were found in the export as received, from the
DICOM headers, before any sorting or processing by dichotomise. They come from
the same Siemens MAGNETOM Terra.X (`syngo MR XA60`) and are examples of the
intermittent errors we have encountered, not an exhaustive list. These occur in product sequences
and C2Ps alike.

### Incident 1: DICOM placed in the wrong series

**TL;DR:** A Series 22 image appeared in the Series 21 folder.

`300.dcm` appeared in the folder for Series 21. Its header gives SeriesNumber 22,
a SeriesInstanceUID different from the other 300 files in the folder, and
InstanceNumber 5. It is the only file with SeriesNumber 22 in that export, so
Series 22 has no folder of its own. The export's folder contradicted the file's
metadata. The same fault was seen again elsewhere: a SeriesNumber 24 file in the
Series 23 folder, with the genuine Series 23 instance absent from the export.

### Incident 2: Duplicated DWI series

**TL;DR:** Two 81-image DWI exports each contained 162 files.

We expected 81 DICOMs in each DWI series (the protocol has 81 diffusion
directions), but each export folder contained 162. Files `1.dcm`-`81.dcm` held
instances 1-81 and files `82.dcm`-`162.dcm` held instances 1-81 again. Each
duplicate pair was byte-identical and shared its SOPInstanceUID. The file
times show the two blocks were written about half an hour apart: the second
pass carried on numbering from the next free integer instead of recognising
instances already on disk. The same happened to a
300-measurement fMRI series (600 files for 300 expected). The filenames `1`-`162`
look like one valid sequence; only the file count or a header audit reveals the
problem.

A separate study had two copies containing 631 and 616 files. The larger
copy contained 15 byte-identical duplicates across seven series; each pair
shared a SOP Instance UID. It also contained a file whose header identified
Series 24 inside the Series 23 folder, where the genuine Series 23 instance
was missing. The files alone do not establish why the two copies differed.
Together, these findings show why a count check and a folder-name check are
not enough: a useful audit needs both content hashes and header metadata.

[Back to the README](../README.md)
