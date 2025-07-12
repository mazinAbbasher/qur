from django import forms
from .models import Employee

class EmployeeForm(forms.ModelForm):
    commission_percentage = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        label="نسبة العمولة (%)",
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'placeholder': 'مثال: 5.00'
        })
    )

    class Meta:
        model = Employee
        fields = ['name', 'commission_percentage']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'اسم المندوب'
            }),
           
        }
        labels = {
            'name': 'اسم المندوب',
            'role': 'الدور الوظيفي',
        }
