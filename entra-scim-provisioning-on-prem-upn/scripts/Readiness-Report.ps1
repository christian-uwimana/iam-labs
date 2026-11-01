# Lists every member of the provisioning group and flags anyone whose extensionAttribute10 is missing or
# doesn't match their on-prem UPN, before Entra tries to provision them.
param([string]$GroupName = 'Netskope Users', [string]$OutFile = '.\netskope-readiness.csv')

Connect-MgGraph -Scopes User.Read.All, GroupMember.Read.All -NoWelcome
$g = Get-MgGroup -Filter "displayName eq '$GroupName'"
$report = Get-MgGroupMember -GroupId $g.Id -All |
  Where-Object { $_.AdditionalProperties['@odata.type'] -eq '#microsoft.graph.user' } |
  ForEach-Object {
    $u = Get-MgUser -UserId $_.Id -Property displayName,userPrincipalName,onPremisesUserPrincipalName,onPremisesExtensionAttributes
    $ext = $u.OnPremisesExtensionAttributes.ExtensionAttribute10
    [pscustomobject]@{
      DisplayName          = $u.DisplayName
      CloudUPN             = $u.UserPrincipalName
      OnPremUPN            = $u.OnPremisesUserPrincipalName
      extensionAttribute10 = $ext
      Ready                = [bool]$ext -and $ext -eq $u.OnPremisesUserPrincipalName
    }
  }
$report | Sort-Object Ready | Format-Table -AutoSize
$report | Export-Csv $OutFile -NoTypeInformation
