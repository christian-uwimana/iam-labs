# Creates the three users who already exist in "Netskope" before provisioning is switched on,
# under their on-prem UPN. Run from any PC after deploy.sh has printed the Tenant URL and token.
$url = (Read-Host "Tenant URL (ends in /scim/v2)").TrimEnd('/')
$token = Read-Host "Secret Token" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($token))
$headers = @{ Authorization = "Bearer $plain" }

$users = @(
    @("kmugisha", "Kevin",   "Mugisha"),
    @("dishimwe", "Diane",   "Ishimwe"),
    @("pnshuti",  "Patrick", "Nshuti")
)
foreach ($u in $users) {
    $body = @{
        schemas  = @("urn:ietf:params:scim:schemas:core:2.0:User")
        userName = "$($u[0])@rwandacorporation.local"
        active   = $true
        name     = @{ givenName = $u[1]; familyName = $u[2] }
    } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "$url/Users" -Headers $headers -ContentType "application/scim+json" -Body $body |
        Select-Object userName, id
}
