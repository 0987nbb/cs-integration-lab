from integration_service.odoo_client.client import OdooClient

client = OdooClient()
fields = client.search_read('ir.model.fields', [['model', '=', 'x_m365_operation'], ['name', '=', 'x_operation_type']], fields=['id'])
if fields:
    client.write('ir.model.fields', [fields[0]['id']], {
        'selection': "[('onboarding', 'Synthetic Onboarding'), ('offboarding', 'Synthetic Offboarding'), ('remediation', 'Helpdesk Remediation'), ('user_360', 'User 360 Diagnostic')]"
    })
    print('Selection updated successfully!')
else:
    print('Field not found.')
