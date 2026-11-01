# The Connect Sync rule

Copies each user's on-premises UPN into `extensionAttribute10` on every sync cycle. New joiners get the value at their first sync, and a UPN change follows on the next cycle. Nothing is written back to Active Directory.

Pick an `extensionAttribute1`-`15` that nobody uses: a rule at precedence 50 wins over a value AD already holds there. In this lab AD has no Exchange schema, so all of them are empty.

## Pause the scheduler

On the Connect Sync server, PowerShell as administrator:

```powershell
Import-Module ADSync
Set-ADSyncScheduler -SyncCycleEnabled $false
Get-ADSyncScheduler | Select-Object SyncCycleEnabled, NextSyncCyclePolicyType
```

## The rule (Synchronization Rules Editor > Inbound > Add new rule)

| Field | Value |
| --- | --- |
| Name | In from AD - User UPN to extensionAttribute10 |
| Description | Copies the on-prem UPN for the Netskope userName |
| Connected System | rwandacorporation.local |
| Connected System Object Type | user |
| Metaverse Object Type | person |
| Link Type | Join |
| Precedence | 50 (lower wins; the default rules start at 100) |
| Scoping filter | none |
| Join rules | none (the built-in In from AD - User Join rule already links the objects) |
| Transformation | FlowType Direct, Target `extensionAttribute10`, Source `userPrincipalName`, Apply Once unticked, Merge Type Update |

No outbound rule is needed: the built-in Out to AAD rule already flows `extensionAttribute10` to Entra. Check it with **View**, not Edit.

## Preview, full sync, resume

Synchronization Service > Connectors > the AD connector > Search Connector Space > a user > Preview > Full synchronization > Generate Preview. Import attribute flow should show the new rule filling `extensionAttribute10`. Then:

```powershell
Start-ADSyncSyncCycle -PolicyType Initial
# wait for the runs in Synchronization Service > Operations to finish
Set-ADSyncScheduler -SyncCycleEnabled $true
```

## Check in Entra

```powershell
Connect-MgGraph -Scopes User.Read.All
Get-MgUser -All -Property displayName,userPrincipalName,onPremisesUserPrincipalName,onPremisesExtensionAttributes |
  Where-Object displayName -match 'Mugisha|Ishimwe|Nshuti|Boardroom' |
  Select-Object displayName, userPrincipalName, onPremisesUserPrincipalName,
    @{n='extensionAttribute10'; e={$_.OnPremisesExtensionAttributes.ExtensionAttribute10}} |
  Format-Table -AutoSize
```
